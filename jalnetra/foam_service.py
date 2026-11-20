"""
jalnetra.foam_service — River foam (foaming) detection inside water.

Python port of river_water_foam_gee.js / .py.
Live water = same MNDWI frequency mask as water_depth_service (permanent/live water).
Foam = nearest clear S2 day spectral detection inside that water. NO depth.

Apr–Sep: foam scene cloud < 70%; other months: < 20%.
Closed LineString KMLs are converted to polygons. Light EE focal_mode only
(no PNG pixel smoother). Foam is solid white on blue water.
"""
from __future__ import annotations

import base64
import math
import xml.etree.ElementTree as ET
from datetime import date
from typing import Any, Dict, List, Optional, Tuple
from xml.dom import minidom

import ee
from pyproj import Transformer
from shapely.geometry import LineString, MultiLineString, MultiPolygon, Polygon
from shapely.ops import transform, unary_union

from jalnetra.ee_http import read_url
from jalnetra.kml_buffer import (
    _geom_to_ee,
    _line_coords_for_kml,
    _utm_epsg,
    load_geometry_from_kml_bytes,
)
from jalnetra.scene_dates import (
    S2_MAX_CLOUD_PCT,
    recent_window,
    select_clear_s2_date,
)

KML_NS = "http://www.opengis.net/kml/2.2"
SCALE = 10
MAX_EXPORT_PIXELS = 8_000_000
MAX_THUMB_DIMENSION = 2048
MAX_CLOUD_PCT = 20
MAX_CLOUD_PCT_APR_SEP = 70
APR_SEP_MONTHS = frozenset({4, 5, 6, 7, 8, 9})
LINE_BUFFER_M = 200

# Live water — same as jalnetra.water_depth_service (MNDWI frequency)
MNDWI_WATER_THRESHOLD = 0.0
LIVE_WATER_FREQ_THRESHOLD = 0.5
CLOSE_M = 20  # morphological close so foam does not leave holes in water

# foam thresholds (same as river_water_foam_gee)
FOAM_THR = 0.2  # share of clear scenes a water pixel must show foam in
VIS_THR = 0.12
NIR_THR = 0.10
WHITE_THR = 0.35
NDWI_THR = 0.2
NDVI_MAX = 0.25
MIN_FOAM_PX = 3
STRICT_CLOUD = False
FOAM_CLOSE_M = 15  # light morph close — do NOT use focal_mode (erases sparse foam)

WATER_BLUE = "0066cc"
FOAM_WHITE = "ffffff"
FOAM_CLASS_PALETTE = [WATER_BLUE, FOAM_WHITE]
KML_OVERLAY_COLOR = "ffffffff"


def _cloud_limit_for_month(month_number: int) -> int:
    """Apr–Sep: cloud < 70%; other months: cloud < 20%."""
    if month_number in APR_SEP_MONTHS:
        return MAX_CLOUD_PCT_APR_SEP
    return MAX_CLOUD_PCT


def _polygon_coords_for_kml(geom) -> List[List[List[float]]]:
    out: List[List[List[float]]] = []
    if isinstance(geom, Polygon):
        out.append([[float(x), float(y)] for x, y in geom.exterior.coords])
    elif isinstance(geom, MultiPolygon):
        for poly in geom.geoms:
            out.append([[float(x), float(y)] for x, y in poly.exterior.coords])
    return out


def _buffer_metres(geom, metres: float):
    clon, clat = float(geom.centroid.x), float(geom.centroid.y)
    epsg = _utm_epsg(clon, clat)
    fwd = Transformer.from_crs("EPSG:4326", f"EPSG:{epsg}", always_xy=True).transform
    inv = Transformer.from_crs(f"EPSG:{epsg}", "EPSG:4326", always_xy=True).transform
    return transform(inv, transform(fwd, geom).buffer(metres))


def _ensure_polygon_aoi(geom):
    """Closed LineString path → polygon so the river interior is included."""
    if isinstance(geom, (Polygon, MultiPolygon)):
        return geom

    if isinstance(geom, LineString):
        coords = list(geom.coords)
        if len(coords) >= 4 and coords[0] == coords[-1]:
            poly = Polygon(coords)
            if poly.is_valid and not poly.is_empty and poly.area > 0:
                return poly
        return _buffer_metres(geom, LINE_BUFFER_M)

    if isinstance(geom, MultiLineString):
        parts = [_ensure_polygon_aoi(part) for part in geom.geoms]
        return unary_union(parts)

    return geom


def foam_geometry(kml_bytes: bytes) -> Tuple[ee.Geometry, Dict[str, Any]]:
    """AOI from KML — closed paths become polygons."""
    raw = load_geometry_from_kml_bytes(kml_bytes)
    geom = _ensure_polygon_aoi(raw)
    clon, clat = float(geom.centroid.x), float(geom.centroid.y)
    aoi_ee = _geom_to_ee(geom)
    meta = {
        "centroid": {"latitude": round(clat, 6), "longitude": round(clon, 6)},
        "geometry_type": geom.geom_type,
        "input_line_coords": _line_coords_for_kml(raw)
        if isinstance(raw, (LineString, MultiLineString))
        else [],
        "input_polygon_coords": _polygon_coords_for_kml(geom),
    }
    return aoi_ee, meta


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


def _estimate_pixels(box: Dict[str, float], scale: float) -> float:
    lat_mid = (box["north"] + box["south"]) / 2.0
    width_m = (box["east"] - box["west"]) * 111320 * math.cos(math.radians(lat_mid))
    height_m = (box["north"] - box["south"]) * 110540
    return (width_m / scale) * (height_m / scale)


def _export_scale_for_box(box: Dict[str, float]) -> float:
    for scale in (10.0, 15.0, 20.0, 30.0):
        if _estimate_pixels(box, scale) <= MAX_EXPORT_PIXELS:
            return scale
    return 30.0


def _thumb_dimensions(box: Dict[str, float], export_scale: float) -> str:
    width_m = max(
        (box["east"] - box["west"])
        * 111320
        * math.cos(math.radians((box["north"] + box["south"]) / 2.0)),
        export_scale,
    )
    height_m = max((box["north"] - box["south"]) * 110540, export_scale)
    width_px = max(1, int(round(width_m / export_scale)))
    height_px = max(1, int(round(height_m / export_scale)))
    longest = max(width_px, height_px)
    if longest > MAX_THUMB_DIMENSION:
        factor = MAX_THUMB_DIMENSION / float(longest)
        width_px = max(1, int(round(width_px * factor)))
        height_px = max(1, int(round(height_px * factor)))
    return f"{width_px}x{height_px}"


def _download_png(
    image: ee.Image,
    geometry: ee.Geometry,
    scale: float,
    *,
    box: Optional[Dict[str, float]] = None,
) -> bytes:
    if box is not None:
        try:
            url = image.getThumbURL(
                {
                    "region": geometry,
                    "dimensions": _thumb_dimensions(box, scale),
                    "format": "png",
                }
            )
            return read_url(url, timeout=180)
        except Exception:
            pass
    try:
        url = image.getDownloadURL(
            {
                "region": geometry,
                "scale": scale,
                "crs": "EPSG:4326",
                "format": "PNG",
            }
        )
        return read_url(url, timeout=300)
    except Exception:
        url = image.getThumbURL(
            {"region": geometry, "scale": scale, "format": "png"}
        )
        return read_url(url, timeout=300)


def _el(parent: ET.Element, tag: str, text: Optional[str] = None) -> ET.Element:
    node = ET.SubElement(parent, f"{{{KML_NS}}}{tag}")
    if text is not None:
        node.text = text
    return node


def _build_kml(
    *,
    title: str,
    description: str,
    box: Dict[str, float],
    overlay_name: str,
    png_bytes: bytes,
    input_polygon_coords: Optional[List[List[List[float]]]] = None,
) -> bytes:
    root = ET.Element(f"{{{KML_NS}}}kml")
    doc = _el(root, "Document")
    _el(doc, "name", title)
    _el(doc, "description", description)

    overlay = _el(doc, "GroundOverlay")
    _el(overlay, "name", overlay_name)
    _el(overlay, "color", KML_OVERLAY_COLOR)
    icon = _el(overlay, "Icon")
    _el(
        icon,
        "href",
        "data:image/png;base64," + base64.b64encode(png_bytes).decode("ascii"),
    )
    llb = _el(overlay, "LatLonBox")
    _el(llb, "north", f"{box['north']:.8f}")
    _el(llb, "south", f"{box['south']:.8f}")
    _el(llb, "east", f"{box['east']:.8f}")
    _el(llb, "west", f"{box['west']:.8f}")

    for idx, coords in enumerate(input_polygon_coords or []):
        pm = _el(doc, "Placemark")
        _el(pm, "name", "Input KML" if idx == 0 else f"Input KML {idx + 1}")
        poly = _el(pm, "Polygon")
        outer = _el(poly, "outerBoundaryIs")
        ring = _el(outer, "LinearRing")
        _el(
            ring,
            "coordinates",
            " ".join(f"{lon},{lat},0" for lon, lat in coords),
        )
        style = _el(pm, "Style")
        ls = _el(style, "LineStyle")
        _el(ls, "color", "ff00ffff")
        _el(ls, "width", "3")
        ps = _el(style, "PolyStyle")
        _el(ps, "color", "00000000")
        _el(ps, "fill", "0")
        _el(ps, "outline", "1")

    xml_bytes = ET.tostring(root, encoding="utf-8")
    return minidom.parseString(xml_bytes).toprettyxml(indent="  ", encoding="utf-8")


def _clear_pixel(img: ee.Image) -> ee.Image:
    s = img.select("SCL")
    return (
        s.eq(1).Or(s.eq(3)).Or(s.eq(8)).Or(s.eq(9)).Or(s.eq(10)).Or(s.eq(11))
    ).Not()


def _foam_ok_mask(img: ee.Image, *, strict_cloud: bool = STRICT_CLOUD) -> ee.Image:
    s = img.select("SCL")
    ok = s.neq(3).And(s.neq(9)).And(s.neq(10))
    if strict_cloud:
        ok = ok.And(s.neq(8)).And(s.neq(11))
    return ok


def _detect_foam(img: ee.Image) -> ee.Image:
    """Binary foam mask from B2/B3/B4/B8 (reflectance 0–1)."""
    r = img.select(["B2", "B3", "B4", "B8"]).divide(10000)
    blue = r.select("B2")
    green = r.select("B3")
    red = r.select("B4")
    nir = r.select("B8")

    vis = blue.add(green).add(red).divide(3)
    spread = blue.max(green).max(red).subtract(blue.min(green).min(red))
    white = spread.divide(vis)
    ndwi = green.subtract(nir).divide(green.add(nir))
    ndvi = nir.subtract(red).divide(nir.add(red))

    foam = (
        vis.gt(VIS_THR)
        .And(nir.gt(NIR_THR))
        .And(white.lt(WHITE_THR))
        .And(ndwi.lt(NDWI_THR))
        .And(ndvi.lt(NDVI_MAX))
    )
    return foam.rename("f")


def _area_ha(mask: ee.Image, geometry: ee.Geometry) -> float:
    result = (
        ee.Image.pixelArea()
        .updateMask(mask)
        .divide(1e4)
        .rename("area")
        .reduceRegion(
            reducer=ee.Reducer.sum(),
            geometry=geometry,
            scale=SCALE,
            maxPixels=1e13,
            bestEffort=True,
        )
    )
    value = ee.Number(
        ee.Algorithms.If(result.contains("area"), result.get("area"), 0)
    ).getInfo()
    return round(float(value or 0), 4)


def _live_water_mask(
    geometry: ee.Geometry,
    *,
    today: Optional[date] = None,
) -> Tuple[ee.Image, str, str]:
    """
    Live / permanent water — same logic as water_depth_service:
    MNDWI frequency over recent_window, freq >= LIVE_WATER_FREQ_THRESHOLD.
    """
    today = today or date.today()
    window_start, window_end = recent_window(today)

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

    def _water_img(img: ee.Image) -> ee.Image:
        w = (
            img.normalizedDifference(["B3", "B11"])
            .gt(MNDWI_WATER_THRESHOLD)
            .rename("w")
        )
        return w.updateMask(_clear_pixel(img))

    freq = s2_window.map(_water_img).mean()
    water = (
        freq.gte(LIVE_WATER_FREQ_THRESHOLD)
        .clip(geometry)
        .selfMask()
        .rename("water")
    )
    # Morphological close (foam GEE) so foam does not leave holes in water
    if CLOSE_M > 0:
        water = (
            water.unmask(0)
            .focal_max(radius=CLOSE_M, units="meters")
            .focal_min(radius=CLOSE_M, units="meters")
            .selfMask()
            .clip(geometry)
            .rename("water")
        )
    return water, window_start, window_end


def analyze_foam(
    aoi_geometry: ee.Geometry,
    *,
    aoi_info: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Live water (water_depth MNDWI frequency) + foam frequency (GEE FOAM_THR).

    Foam spectral thresholds unchanged; foam = share of clear scenes ≥ FOAM_THR
    inside live water (same as river_water_foam_gee.js). Nearest clear day is
    still reported as image_date.
    """
    today = date.today()
    aoi_info = aoi_info or {}
    cloud_limit = _cloud_limit_for_month(today.month)
    window_start, window_end = recent_window(today)

    # ---- 1. live water (same as /api/water-depth) ----
    water, window_start, window_end = _live_water_mask(aoi_geometry, today=today)

    # Nearest clear day (metadata / reference only)
    scene = select_clear_s2_date(
        aoi_geometry,
        today=today,
        max_cloud_pct=cloud_limit,
    )
    image_date = scene.get("selected_date") or scene.get("start_date")
    tile_cloud = scene.get("tile_cloud_percent")

    # ---- 2. foam frequency over look-back window (original GEE logic) ----
    # Single-day foam was ~0 ha; GEE uses mean(foam) >= FOAM_THR across scenes.
    foam_col = (
        ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
        .filterBounds(aoi_geometry)
        .filterDate(window_start, window_end)
        .filter(ee.Filter.lt("CLOUDY_PIXEL_PERCENTAGE", S2_MAX_CLOUD_PCT))
    )
    if int(foam_col.size().getInfo() or 0) == 0:
        raise ValueError(
            f"No Sentinel-2 scenes for foam between {window_start} and {window_end}."
        )

    def _foam_img(img: ee.Image) -> ee.Image:
        return (
            _detect_foam(img)
            .updateMask(_foam_ok_mask(img))
            .updateMask(water)
            .rename("f")
        )

    foam_freq = foam_col.map(_foam_img).mean()
    foam = foam_freq.gte(FOAM_THR).selfMask().rename("foam")
    if MIN_FOAM_PX > 1:
        foam = foam.updateMask(
            foam.connectedPixelCount(100, True).gte(MIN_FOAM_PX)
        )
    # Light morph close — keeps foam patches; focal_mode was wiping sparse foam
    if FOAM_CLOSE_M > 0:
        foam = (
            foam.unmask(0)
            .focal_max(radius=FOAM_CLOSE_M, units="meters")
            .focal_min(radius=FOAM_CLOSE_M, units="meters")
            .updateMask(water)
            .selfMask()
            .rename("foam")
        )
    foam = foam.clip(aoi_geometry).rename("foam")

    foam_class = (
        water.unmask(0)
        .multiply(0)
        .where(foam.unmask(0).eq(1), 1)
        .updateMask(water)
        .clip(aoi_geometry)
        .rename("foam_class")
    )

    water_ha = _area_ha(water, aoi_geometry)
    if water_ha <= 0:
        raise ValueError(
            f"No live water detected in the KML area "
            f"({window_start} to {window_end})."
        )
    foam_ha = _area_ha(foam, aoi_geometry)
    foam_pct = round((foam_ha / water_ha * 100.0), 2) if water_ha > 0 else 0.0

    box = _aoi_box(aoi_geometry)
    export_scale = _export_scale_for_box(box)
    scaled = foam_class.reproject(crs="EPSG:4326", scale=export_scale)
    foam_vis = scaled.visualize(
        min=0, max=1, palette=FOAM_CLASS_PALETTE
    ).updateMask(scaled.mask())
    png = _download_png(foam_vis, aoi_geometry, export_scale, box=box)

    kml_filename = f"river_foam_{image_date}.kml"
    kml_bytes = _build_kml(
        title=f"River Foam — {image_date}",
        description=(
            f"Live water (MNDWI freq>={LIVE_WATER_FREQ_THRESHOLD}) + "
            f"foam frequency (FOAM_THR>={FOAM_THR}) over {window_start}–{window_end}.\n"
            f"Nearest clear day: {image_date} (tile cloud {tile_cloud}%).\n"
            f"Water: {water_ha} ha. Foam: {foam_ha} ha ({foam_pct}% of water)."
        ),
        box=box,
        overlay_name="Foam (white) / Live water (blue)",
        png_bytes=png,
        input_polygon_coords=aoi_info.get("input_polygon_coords"),
    )

    result: Dict[str, Any] = {
        "analysis_date": today.isoformat(),
        "image_date": image_date,
        "tile_cloud_percent": tile_cloud,
        "max_cloud_percent": cloud_limit,
        "water_area_ha": water_ha,
        "foam_area_ha": foam_ha,
        "foam_percent_of_water": foam_pct,
        "export_scale_m": export_scale,
        "scene_selection": scene,
        "live_water": {
            "mndwi_threshold": MNDWI_WATER_THRESHOLD,
            "frequency_threshold": LIVE_WATER_FREQ_THRESHOLD,
            "window_start": window_start,
            "window_end": window_end,
            "source": "water_depth_service (MNDWI frequency)",
        },
        "parameters": {
            "mndwi_threshold": MNDWI_WATER_THRESHOLD,
            "live_water_freq_threshold": LIVE_WATER_FREQ_THRESHOLD,
            "foam_thr": FOAM_THR,
            "close_m": CLOSE_M,
            "foam_close_m": FOAM_CLOSE_M,
            "vis_thr": VIS_THR,
            "nir_thr": NIR_THR,
            "white_thr": WHITE_THR,
            "ndwi_thr": NDWI_THR,
            "ndvi_max": NDVI_MAX,
            "min_foam_px": MIN_FOAM_PX,
            "strict_cloud": STRICT_CLOUD,
            "scale_m": SCALE,
            "max_cloud_percent": cloud_limit,
        },
        "legend": {
            "water_no_foam": f"#{WATER_BLUE}",
            "foam": f"#{FOAM_WHITE}",
        },
        "kml_bytes": kml_bytes,
        "kml_filename": kml_filename,
        "notes": {
            "geometry": "Closed LineString KMLs are converted to polygons.",
            "live_water": (
                "Same MNDWI frequency live-water mask as POST /api/water-depth."
            ),
            "foam": (
                f"Foam frequency over look-back window (FOAM_THR>={FOAM_THR}); "
                "same spectral thresholds as river_water_foam_gee. "
                "focal_mode removed — it erased sparse foam into water."
            ),
        },
    }
    if aoi_info:
        result["aoi"] = {
            k: v
            for k, v in aoi_info.items()
            if k not in ("input_line_coords", "input_polygon_coords")
        }
    return result
