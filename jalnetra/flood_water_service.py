"""
jalnetra.flood_water_service — Flood/water datewise overlays for JalNetra API.

Wraps flood_tile Sentinel-1 logic with:
  - 50 m border buffer
  - sequential image pre/post pairs
  - smoothed KML overlays (no square borders)
  - class-wise flood/water areas only (no lat/lon, no Excel)
"""
from __future__ import annotations

import base64
import math
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional, Tuple
from xml.dom import minidom

import ee

from jalnetra.ee_http import read_url
from jalnetra.flood_deps.flood_service import (
    _build_classification,
    _compute_flood_water_areas_ha,
    _s1_collection,
)
from jalnetra.flood_deps.speckle_filters import refined_lee, to_db, to_natural
from jalnetra.flood_deps.s1_scenes import image_for_millis, scene_records

KML_NS = "http://www.opengis.net/kml/2.2"
BUFFER_METERS = 50
DISPLAY_SCALE_M = 8
DISPLAY_SCALE_FALLBACK = [10.0, 15.0]
SMOOTH_RADIUS_M = 45
MASK_SMOOTH_M = 25
MAX_EXPORT_PIXELS = 8_000_000
KML_OVERLAY_COLOR = "ffffffff"
# 0 = other (transparent), 1 = water (blue), 2 = flood (red)
FLOOD_PALETTE = ["000000", "0000FF", "FF0000"]
_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="jal_flood_")


def buffered_flood_geometry(geometry: ee.Geometry) -> ee.Geometry:
    """50 m geodesic buffer from KML border."""
    return geometry.buffer(BUFFER_METERS)


def _aoi_box(geometry: ee.Geometry) -> Dict[str, float]:
    coords = geometry.bounds().getInfo()["coordinates"][0]
    lons = [c[0] for c in coords]
    lats = [c[1] for c in coords]
    return {
        "north": max(lats),
        "south": min(lats),
        "east": max(lons),
        "west": min(lons),
    }


def _estimate_pixel_count(box: Dict[str, float], scale: float) -> float:
    lat_mid = (box["north"] + box["south"]) / 2.0
    width_m = (box["east"] - box["west"]) * 111320 * math.cos(math.radians(lat_mid))
    height_m = (box["north"] - box["south"]) * 110540
    return (width_m / scale) * (height_m / scale)


def _export_scale_for_geometry(geometry: ee.Geometry) -> float:
    box = _aoi_box(geometry)
    for candidate in (DISPLAY_SCALE_M, *DISPLAY_SCALE_FALLBACK, 20.0):
        if _estimate_pixel_count(box, candidate) <= MAX_EXPORT_PIXELS:
            return candidate
    return 20.0


def _smooth_classified(image: ee.Image) -> ee.Image:
    first = image.focal_mode(radius=SMOOTH_RADIUS_M, units="meters")
    return first.focal_mode(radius=max(SMOOTH_RADIUS_M // 2, 12), units="meters")


def _smooth_mask(mask: ee.Image) -> ee.Image:
    return mask.focal_max(radius=MASK_SMOOTH_M, units="meters").focal_min(
        radius=MASK_SMOOTH_M, units="meters"
    )


def _vis_image(classification: ee.Image, aoi: ee.Geometry) -> ee.Image:
    export_scale = _export_scale_for_geometry(aoi)
    clipped = classification.clip(aoi)
    # Keep only water (1) and flood (2) — transparent elsewhere
    region = _smooth_mask(clipped.gt(0))
    smoothed = _smooth_classified(clipped)
    masked = smoothed.updateMask(region).clip(aoi)
    scaled = masked.reproject(crs="EPSG:4326", scale=export_scale)
    return scaled.visualize(min=0, max=2, palette=FLOOD_PALETTE).updateMask(
        scaled.mask().And(scaled.neq(0))
    )


def _export_overlay_png(vis_image: ee.Image, geometry: ee.Geometry) -> bytes:
    export_scale = _export_scale_for_geometry(geometry)
    try:
        url = vis_image.getDownloadURL(
            {
                "region": geometry,
                "scale": export_scale,
                "crs": "EPSG:4326",
                "format": "PNG",
            }
        )
        return read_url(url, timeout=900)
    except Exception:
        url = vis_image.getThumbURL(
            {"region": geometry, "scale": export_scale, "format": "png"}
        )
        return read_url(url, timeout=900)


def _kml_el(parent: ET.Element, tag: str, text: Optional[str] = None) -> ET.Element:
    el = ET.SubElement(parent, f"{{{KML_NS}}}{tag}")
    if text is not None:
        el.text = text
    return el


def _build_kml(
    *,
    title: str,
    description: str,
    box: Dict[str, float],
    overlay_name: str,
    png_bytes: bytes,
) -> bytes:
    root = ET.Element(f"{{{KML_NS}}}kml")
    doc = _kml_el(root, "Document")
    _kml_el(doc, "name", title)
    _kml_el(doc, "description", description)
    overlay = _kml_el(doc, "GroundOverlay")
    _kml_el(overlay, "name", overlay_name)
    _kml_el(overlay, "color", KML_OVERLAY_COLOR)
    icon = _kml_el(overlay, "Icon")
    b64 = base64.b64encode(png_bytes).decode("ascii")
    _kml_el(icon, "href", f"data:image/png;base64,{b64}")
    llb = _kml_el(overlay, "LatLonBox")
    _kml_el(llb, "north", f"{box['north']:.8f}")
    _kml_el(llb, "south", f"{box['south']:.8f}")
    _kml_el(llb, "east", f"{box['east']:.8f}")
    _kml_el(llb, "west", f"{box['west']:.8f}")
    xml_bytes = ET.tostring(root, encoding="utf-8")
    return minidom.parseString(xml_bytes).toprettyxml(indent="  ", encoding="utf-8")


def _last_scene_before_date(
    collection: ee.ImageCollection, geometry: ee.Geometry, date_str: str
) -> Optional[Tuple[ee.Image, str, int]]:
    """Last Sentinel-1 scene strictly before date_str."""
    filtered = (
        collection.filter(ee.Filter.date("1970-01-01", date_str))
        .filterBounds(geometry)
        .sort("system:time_start", False)
    )
    if int(filtered.size().getInfo() or 0) == 0:
        return None
    first = filtered.first()
    img = ee.Image(first).select("VH").clip(geometry)
    millis = int(first.get("system:time_start").getInfo())
    pre_date = ee.Date(millis).format("YYYY-MM-dd").getInfo()
    return img, pre_date, millis


def _build_sequential_pairs(
    collection: ee.ImageCollection,
    geometry: ee.Geometry,
    scenes: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """
    Pre/post from image dates only:
      - 1st image: pre = previous S1 before that image
      - later images: pre = previous image in the series
    """
    pairs: List[Dict[str, Any]] = []
    for idx, post_scene in enumerate(scenes):
        if idx == 0:
            pre_lookup = _last_scene_before_date(
                collection, geometry, post_scene["date"]
            )
            if pre_lookup is None:
                continue
            pre_image, pre_date, _ = pre_lookup
        else:
            pre_scene = scenes[idx - 1]
            pre_image = image_for_millis(collection, geometry, pre_scene["millis"])
            pre_date = pre_scene["date"]

        post_image = image_for_millis(collection, geometry, post_scene["millis"])
        pairs.append(
            {
                "pre_image": pre_image,
                "post_image": post_image,
                "pre_date": pre_date,
                "post_date": post_scene["date"],
                "post_millis": post_scene["millis"],
            }
        )
    return pairs


def _analyze_pair_with_kml(
    pre_image: ee.Image,
    post_image: ee.Image,
    geometry: ee.Geometry,
    pre_date: str,
    post_date: str,
    box: Dict[str, float],
) -> Dict[str, Any]:
    before_filtered = ee.Image(to_db(refined_lee(to_natural(pre_image))))
    after_filtered = ee.Image(to_db(refined_lee(to_natural(post_image))))
    classification, water_mask, flood_mask = _build_classification(
        before_filtered, after_filtered, geometry
    )
    water_ha, flood_ha = _compute_flood_water_areas_ha(water_mask, flood_mask, geometry)

    vis = _vis_image(classification, geometry)
    png = _export_overlay_png(vis, geometry)
    kml_bytes = _build_kml(
        title=f"Flood/Water {post_date}",
        description=(
            f"Sentinel-1 VH flood/water · pre={pre_date} · post={post_date}\n"
            f"Water: {water_ha} ha · Flood: {flood_ha} ha\n"
            "Blue = permanent/common water · Red = flood"
        ),
        box=box,
        overlay_name=f"Flood {post_date}",
        png_bytes=png,
    )
    return {
        "pre_date": pre_date,
        "post_date": post_date,
        "date": post_date,
        "water_area_ha": water_ha,
        "flood_area_ha": flood_ha,
        "categories": [
            {
                "id": 1,
                "name": "Water",
                "color": "#0000FF",
                "area_ha": water_ha,
            },
            {
                "id": 2,
                "name": "Flood",
                "color": "#FF0000",
                "area_ha": flood_ha,
            },
        ],
        "kml_bytes": kml_bytes,
        "kml_filename": f"flood_water_{post_date}.kml",
    }


def analyze_flood_water_datewise(
    geometry: ee.Geometry,
    start_date: str,
    end_date: str,
) -> Dict[str, Any]:
    """
    Buffer AOI by 50 m, run sequential S1 pairs, return class areas + KMLs.
    """
    aoi = buffered_flood_geometry(geometry)
    box = _aoi_box(aoi)
    collection = _s1_collection()
    scenes = scene_records(collection, aoi, start_date, end_date)
    if not scenes:
        raise ValueError("No Sentinel-1 scenes found in the selected date range.")

    pairs = _build_sequential_pairs(collection, aoi, scenes)
    if not pairs:
        raise ValueError(
            "No pre/post image pairs could be built "
            "(need a prior Sentinel-1 scene before the first image)."
        )

    datewise: List[Dict[str, Any]] = []
    futures = {
        _executor.submit(
            _analyze_pair_with_kml,
            p["pre_image"],
            p["post_image"],
            aoi,
            p["pre_date"],
            p["post_date"],
            box,
        ): p["post_date"]
        for p in pairs
    }
    by_date: Dict[str, Dict[str, Any]] = {}
    for future in as_completed(futures):
        by_date[futures[future]] = future.result()

    for p in pairs:
        datewise.append(by_date[p["post_date"]])

    return {
        "start_date": start_date,
        "end_date": end_date,
        "buffer_m": BUFFER_METERS,
        "scene_count": len(scenes),
        "comparison_count": len(datewise),
        "datewise": datewise,
        "logic": (
            "Per-scene sequential Sentinel-1 VH pairs (image dates only): "
            "1st image pre = previous S1 before that date; "
            "later images pre = previous image in the series. "
            "water = VH < -20 dB both scenes; "
            "flood = VH > -20 dB pre and < -20 dB post."
        ),
        "notes": {
            "buffer": "50 m from KML border",
            "areas": "Class-wise water and flood hectares only (not whole KML area)",
            "smoothing": "focal_mode + fine export grid — no square borders",
        },
    }
