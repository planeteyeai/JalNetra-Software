"""
JalNetra live water-depth analysis.

Input: an EE geometry derived from a KML polygon.
Output: two KMLs —
  - permanent / SAR water extent in blue
  - Sentinel-2 relative depth in green -> yellow -> orange -> red

Depth is a relative optical band-ratio estimate, not survey-grade bathymetry.
"""
from __future__ import annotations

import base64
import os
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from xml.dom import minidom

import ee

KML_NS = "http://www.opengis.net/kml/2.2"
MAX_EXPORT_PIXELS = 8_000_000
WATER_BLUE = "0000FF"
DEPTH_PALETTE = ["00A651", "FFFF00", "FFA500", "FF0000"]


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
    with urllib.request.urlopen(url, timeout=900) as resp:
        return resp.read()


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


def analyze_live_water_depth(geometry: ee.Geometry) -> Dict[str, Any]:
    today = date.today()
    # Recent windows are deliberately wider than a single revisit so sparse Sentinel scenes can be found.
    s1_start = today - timedelta(days=int(os.getenv("WATER_DEPTH_S1_DAYS", "12")))
    s2_start = today - timedelta(days=int(os.getenv("WATER_DEPTH_S2_DAYS", "30")))
    s1_end = today + timedelta(days=1)
    s2_end = today + timedelta(days=1)

    s1 = (ee.ImageCollection("COPERNICUS/S1_GRD")
          .filterBounds(geometry)
          .filterDate(s1_start.isoformat(), s1_end.isoformat())
          .filter(ee.Filter.listContains("transmitterReceiverPolarisation", "VV"))
          .filter(ee.Filter.eq("instrumentMode", "IW"))
          .select("VV"))
    if int(s1.size().getInfo() or 0) == 0:
        raise ValueError(f"No Sentinel-1 VV image found in the live window {s1_start} to {today}.")

    s1_image_dates = _ee_ymd_list(s1)
    s1_image_date = _ee_ymd(ee.Image(s1.sort("system:time_start", False).first()))

    # Latest available S1 mosaic, followed by focal median smoothing as in the supplied workflow.
    water_db = s1.sort("system:time_start", False).mosaic().clip(geometry)
    water = (water_db.focal_median(30, "circle", "meters")
             .lt(-17).rename("water_mask").selfMask().clip(geometry))

    s2 = (ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
          .filterBounds(geometry)
          .filterDate(s2_start.isoformat(), s2_end.isoformat())
          .filter(ee.Filter.lt("CLOUDY_PIXEL_PERCENTAGE", 80))
          .select(["B2", "B3", "SCL"])
          .sort("CLOUDY_PIXEL_PERCENTAGE"))
    if int(s2.size().getInfo() or 0) == 0:
        raise ValueError(f"No Sentinel-2 image found in the live window {s2_start} to {today}.")

    s2_best_raw = ee.Image(s2.first())
    s2_image_date = _ee_ymd(s2_best_raw)
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

    depth_min = float(os.getenv("WATER_DEPTH_MIN_M", "1.5"))
    depth_max = float(os.getenv("WATER_DEPTH_MAX_M", "2.0"))
    depth = (ratio_water.unitScale(p_low, p_high).clamp(0, 1)
             .multiply(depth_max - depth_min).add(depth_min).rename("depth_m").clip(geometry))

    # Fill optical gaps inside SAR water extent, then smooth the depth surface.
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
        f"Permanent Water - {s1_image_date or today.isoformat()}",
        f"Sentinel-1 SAR water extent (blue). Image date: {s1_image_date or 'unknown'}. "
        f"Water area: {water_area_ha} ha.",
        box,
        "Permanent Water - Blue",
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
        "sentinel1_image_date": s1_image_date,
        "sentinel1_image_dates": s1_image_dates,
        "sentinel2_image_date": s2_image_date,
        "sentinel1_window": {"start": s1_start.isoformat(), "end": today.isoformat()},
        "sentinel2_window": {"start": s2_start.isoformat(), "end": today.isoformat()},
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
        "water_kml_filename": f"permanent_water_{s1_image_date or today.isoformat()}.kml",
        "depth_kml_filename": f"water_depth_{s2_image_date or today.isoformat()}.kml",
    }
