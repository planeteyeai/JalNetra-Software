"""
JalNetra live water-depth analysis.

Input: an EE geometry derived from a KML polygon.
Output: two KMLs —
  - live water extent (Sentinel-2 MNDWI frequency) in blue
  - Sentinel-2 relative depth in green -> yellow -> orange -> red

Depth is a relative optical band-ratio estimate, not survey-grade bathymetry.
"""
from __future__ import annotations

import base64
import os
import xml.etree.ElementTree as ET
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional
from xml.dom import minidom

import ee

from jalnetra.ee_http import read_url
from jalnetra.scene_dates import (
    S2_MAX_CLOUD_PCT,
    recent_window,
    select_clear_s2_date,
)

KML_NS = "http://www.opengis.net/kml/2.2"
MAX_EXPORT_PIXELS = 8_000_000
WATER_BLUE = "0000FF"
DEPTH_PALETTE = ["00A651", "FFFF00", "FFA500", "FF0000"]
# Live water = share of clear S2 scenes with MNDWI above threshold (river_depth_gee).
MNDWI_WATER_THRESHOLD = 0.0
LIVE_WATER_FREQ_THRESHOLD = 0.5


def _aoi_box(geometry: ee.Geometry) -> Dict[str, float]:
    coords = geometry.bounds().getInfo()["coordinates"][0]
    lons = [c[0] for c in coords]
    lats = [c[1] for c in coords]
    return {"north": max(lats), "south": min(lats), "east": max(lons), "west": min(lons)}


def _estimate_pixels(box: Dict[str, float], scale: float) -> float:
    import math
    lat_mid = (box["north"] + box["south"]) / 2
    width_m = (box["east"] - box["west"]) * 111320 * math.cos(math.radians(lat_mid))
    height_m = (box["north"] - box["south"]) * 110540
    return (width_m / scale) * (height_m / scale)


def _export_scale(geometry: ee.Geometry) -> float:
    box = _aoi_box(geometry)
    for scale in (10.0, 15.0, 20.0, 30.0):
        if _estimate_pixels(box, scale) <= MAX_EXPORT_PIXELS:
            return scale
    return 30.0


def _download_png(image: ee.Image, geometry: ee.Geometry, scale: float) -> bytes:
    params = {
        "region": geometry,
        "scale": scale,
        "crs": "EPSG:4326",
        "format": "PNG",
    }
    try:
        url = image.getDownloadURL(params)
    except Exception:
        url = image.getThumbURL({"region": geometry, "scale": scale, "format": "png"})
    return read_url(url, timeout=900)


def _el(parent: ET.Element, tag: str, text: str | None = None) -> ET.Element:
    node = ET.SubElement(parent, f"{{{KML_NS}}}{tag}")
    if text is not None:
        node.text = text
    return node


def _overlay(doc: ET.Element, name: str, png: bytes, box: Dict[str, float], opacity: str = "ff") -> None:
    overlay = _el(doc, "GroundOverlay")
    _el(overlay, "name", name)
    _el(overlay, "color", opacity + "ffffff")
    icon = _el(overlay, "Icon")
    _el(icon, "href", "data:image/png;base64," + base64.b64encode(png).decode("ascii"))
    llb = _el(overlay, "LatLonBox")
    _el(llb, "north", f"{box['north']:.8f}")
    _el(llb, "south", f"{box['south']:.8f}")
    _el(llb, "east", f"{box['east']:.8f}")
    _el(llb, "west", f"{box['west']:.8f}")


def _ee_ymd(image: ee.Image) -> Optional[str]:
    """Read YYYY-MM-DD from an Earth Engine image timestamp."""
    try:
        return ee.Date(image.get("system:time_start")).format("YYYY-MM-dd").getInfo()
    except Exception:
        return None


def _ee_ymd_list(collection: ee.ImageCollection) -> List[str]:
    """Unique image dates in a collection, oldest first."""
    try:
        millis = collection.aggregate_array("system:time_start").getInfo() or []
    except Exception:
        return []
    dates = set()
    for value in millis:
        try:
            dates.add(
                datetime.fromtimestamp(float(value) / 1000.0, tz=timezone.utc).strftime(
                    "%Y-%m-%d"
                )
            )
        except (TypeError, ValueError, OSError):
            continue
    return sorted(dates)


def _build_kml(title: str, description: str, box: Dict[str, float], overlay_name: str, png: bytes) -> bytes:
    root = ET.Element(f"{{{KML_NS}}}kml")
    doc = _el(root, "Document")
    _el(doc, "name", title)
    _el(doc, "description", description)
    _overlay(doc, overlay_name, png, box, "cc")
    xml_bytes = ET.tostring(root, encoding="utf-8")
    return minidom.parseString(xml_bytes).toprettyxml(indent="  ", encoding="utf-8")


def analyze_live_water_depth(
    geometry: ee.Geometry,
    depth_min_m: Optional[float] = None,
    depth_max_m: Optional[float] = None,
) -> Dict[str, Any]:
    today = date.today()
    s2_selection = select_clear_s2_date(geometry, today=today)
    window_start, window_end = recent_window(today)

    # Live water: Sentinel-2 MNDWI frequency over the look-back window
    # (same logic as river_depth_gee water mask / freq_thr).
    s2_window = (
        ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
        .filterBounds(geometry)
        .filterDate(window_start, window_end)
        .filter(ee.Filter.lt("CLOUDY_PIXEL_PERCENTAGE", S2_MAX_CLOUD_PCT))
    )
    if int(s2_window.size().getInfo() or 0) == 0:
        raise ValueError(
            f"No Sentinel-2 scenes found between {window_start} and {window_end}."
        )

    def _clear_pixel(img: ee.Image) -> ee.Image:
        s = img.select("SCL")
        return (
            s.eq(1).Or(s.eq(3)).Or(s.eq(8)).Or(s.eq(9)).Or(s.eq(10)).Or(s.eq(11))
        ).Not()

    def _water_img(img: ee.Image) -> ee.Image:
        w = img.normalizedDifference(["B3", "B11"]).gt(MNDWI_WATER_THRESHOLD).rename("w")
        return w.updateMask(_clear_pixel(img))

    freq = s2_window.map(_water_img).mean()
    water = (
        freq.gte(LIVE_WATER_FREQ_THRESHOLD)
        .clip(geometry)
        .selfMask()
        .rename("water_mask")
    )

    s2 = (
        ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
        .filterBounds(geometry)
        .filterDate(s2_selection["start_date"], s2_selection["end_date"])
        .filter(ee.Filter.lte("CLOUDY_PIXEL_PERCENTAGE", S2_MAX_CLOUD_PCT))
        .select(["B2", "B3", "SCL"])
        .sort("CLOUDY_PIXEL_PERCENTAGE")
    )
    if int(s2.size().getInfo() or 0) == 0:
        raise ValueError(
            f"No Sentinel-2 image found on {s2_selection['selected_date']}."
        )

    s2_best_raw = ee.Image(s2.first())
    s2_image_date = _ee_ymd(s2_best_raw)
    s2_image_dates = _ee_ymd_list(s2_window)
    s2_best = s2_best_raw.clip(geometry)
    scl = s2_best.select("SCL")
    cloud_mask = scl.neq(3).And(scl.neq(8)).And(scl.neq(9)).And(scl.neq(10))
    clean = s2_best.updateMask(cloud_mask)
    valid = clean.select("B2").gt(0).And(clean.select("B3").gt(0))
    ratio = clean.updateMask(valid).select("B2").divide(clean.updateMask(valid).select("B3")).rename("ratio")
    ratio_water = ratio.updateMask(water)

    percentiles = ratio_water.reduceRegion(
        reducer=ee.Reducer.percentile([2, 98]), geometry=geometry, scale=10, maxPixels=1e13
    )
    p_low = ee.Number(percentiles.get("ratio_p2"))
    p_high = ee.Number(percentiles.get("ratio_p98"))

    depth_min = (
        float(depth_min_m) if depth_min_m is not None
        else float(os.getenv("WATER_DEPTH_MIN_M", "1.5"))
    )
    depth_max = (
        float(depth_max_m) if depth_max_m is not None
        else float(os.getenv("WATER_DEPTH_MAX_M", "2.0"))
    )
    if depth_max <= depth_min:
        raise ValueError("Maximum depth must be greater than minimum depth.")
    depth = (ratio_water.unitScale(p_low, p_high).clamp(0, 1)
             .multiply(depth_max - depth_min).add(depth_min).rename("depth_m").clip(geometry))

    # Fill optical gaps inside live-water extent, then smooth the depth surface.
    fill1 = depth.reduceNeighborhood(ee.Reducer.mean(), ee.Kernel.circle(radius=50, units="meters"), None, True)
    depth1 = depth.unmask(fill1)
    fill2 = depth1.reduceNeighborhood(ee.Reducer.mean(), ee.Kernel.circle(radius=150, units="meters"), None, True)
    depth2 = depth1.unmask(fill2)
    mean_depth = ee.Number(depth2.reduceRegion(
        reducer=ee.Reducer.mean(), geometry=geometry, scale=10, maxPixels=1e13
    ).get("depth_m"))
    depth = (depth2.unmask(mean_depth)
             .rename("depth_m").updateMask(water).clip(geometry)
             .reduceNeighborhood(ee.Reducer.mean(), ee.Kernel.circle(radius=20, units="meters"), None, True)
             .rename("depth_m").updateMask(water).clip(geometry))

    scale = _export_scale(geometry)
    water_vis = water.visualize(min=0, max=1, palette=[WATER_BLUE]).updateMask(water)
    depth_class = depth.subtract(depth_min).divide((depth_max - depth_min) / 4).floor().clamp(0, 3)
    depth_vis = depth_class.visualize(min=0, max=3, palette=DEPTH_PALETTE).updateMask(depth.mask())
    box = _aoi_box(geometry)
    water_png = _download_png(water_vis, geometry, scale)
    depth_png = _download_png(depth_vis, geometry, scale)

    stats = depth.reduceRegion(
        reducer=ee.Reducer.min().combine(ee.Reducer.max(), sharedInputs=True).combine(ee.Reducer.mean(), sharedInputs=True),
        geometry=geometry, scale=10, maxPixels=1e13,
    ).getInfo()
    water_area_ha = ee.Image.pixelArea().updateMask(water).divide(10000).reduceRegion(
        reducer=ee.Reducer.sum(), geometry=geometry, scale=10, maxPixels=1e13
    ).getInfo().get("area")

    water_kml = _build_kml(
        f"Live Water - {s2_image_date or today.isoformat()}",
        f"Sentinel-2 MNDWI live water (blue). Frequency threshold: "
        f"{LIVE_WATER_FREQ_THRESHOLD}. Window: {s2_selection['window_start']} to "
        f"{s2_selection['window_end']}. Water area: {water_area_ha} ha.",
        box,
        "Live Water - Blue",
        water_png,
    )
    depth_kml = _build_kml(
        f"Water Depth - {s2_image_date or today.isoformat()}",
        f"Sentinel-2 relative depth. Image date: {s2_image_date or 'unknown'}. "
        f"Analysis date: {today.isoformat()}. Water area: {water_area_ha} ha. "
        f"Depth range: {depth_min}-{depth_max} m. "
        "Depth is a relative band-ratio estimate and is not survey-grade bathymetry.",
        box,
        "Water Depth - Green to Red",
        depth_png,
    )
    return {
        "analysis_date": today.isoformat(),
        "image_date": s2_image_date,
        "sentinel2_image_date": s2_image_date,
        "sentinel2_image_dates": s2_image_dates,
        "sentinel2_window": {
            "start": s2_selection["window_start"], "end": s2_selection["window_end"]
        },
        "sentinel2_selection": s2_selection,
        "live_water": {
            "mndwi_threshold": MNDWI_WATER_THRESHOLD,
            "frequency_threshold": LIVE_WATER_FREQ_THRESHOLD,
            "window_start": s2_selection["window_start"],
            "window_end": s2_selection["window_end"],
        },
        "input_depth_range_m": {"min": depth_min, "max": depth_max},
        "water_area_ha": water_area_ha,
        "depth": {"min_m": stats.get("depth_m_min"), "max_m": stats.get("depth_m_max"), "mean_m": stats.get("depth_m_mean")},
        "legend": {
            "water": "#0000FF",
            "low_depth": "#00A651",
            "medium_low_depth": "#FFFF00",
            "medium_high_depth": "#FFA500",
            "high_depth": "#FF0000",
        },
        "water_kml_bytes": water_kml,
        "depth_kml_bytes": depth_kml,
        "water_kml_filename": f"live_water_{s2_image_date or today.isoformat()}.kml",
        "depth_kml_filename": f"water_depth_{s2_image_date or today.isoformat()}.kml",
    }
