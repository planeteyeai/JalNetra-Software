"""
jalnetra.silt_service — Monthly silt classification (Jan → current month).

Python port of the Mithi River monthly silt GEE workflow (Sentinel-2 only).
No buffer. Returns one smoothed KML per month that has imagery.
"""
from __future__ import annotations

import base64
import math
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from typing import Any, Dict, List, Optional, Tuple
from xml.dom import minidom

import ee
from shapely.geometry import MultiPolygon, Polygon

from jalnetra.ee_http import read_url
from jalnetra.kml_buffer import (
    _geom_to_ee,
    _line_coords_for_kml,
    load_geometry_from_kml_bytes,
)

KML_NS = "http://www.opengis.net/kml/2.2"
YEAR = 2026
SCALE = 20
DISPLAY_SCALE_M = 5
DISPLAY_SCALE_FALLBACK = [8.0, 10.0]
SMOOTH_RADIUS_M = 70
MASK_SMOOTH_M = 40
MAX_EXPORT_PIXELS = 12_000_000
MAX_PIXELS = 1e13
TILE_SCALE = 4
KML_OVERLAY_COLOR = "ffffffff"
MAX_CLOUD_S2 = 40

# Silt volume surface = silt score × pixel area (10 m grid, EPSG:4326) × layer
# thickness. Matches the reference "Silt Volume Surface" KMZs (0–94.31 scale
# over Pune = one 10 m EPSG:4326 pixel area in m²).
VOLUME_PIXEL_SCALE_M = 10
SILT_LAYER_THICKNESS_M = 1.0
VOLUME_PALETTE = [  # ColorBrewer YlOrBr (earth tone)
    "FFFFE5", "FFF7BC", "FEE391", "FEC44F", "FE9929",
    "EC7014", "CC4C02", "993404", "662506",
]

_MONTH_NAMES = [
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
]

CLASS_PALETTE = ["00FF00", "FFFF00", "FFA500", "FF0000"]

SILT_CLASSES: List[Dict[str, Any]] = [
    {"id": 1, "name": "Low Silt", "range": "score ≤ 0.25", "color": "#00FF00"},
    {"id": 2, "name": "Moderate Silt", "range": "0.25–0.50", "color": "#FFFF00"},
    {"id": 3, "name": "High Silt", "range": "0.50–0.75", "color": "#FFA500"},
    {"id": 4, "name": "Very High Silt", "range": "score > 0.75", "color": "#FF0000"},
]


def active_months(today: Optional[date] = None) -> List[Tuple[int, str]]:
    """January → current month for YEAR (or through December if past YEAR)."""
    today = today or date.today()
    if today.year > YEAR:
        end_month = 12
    elif today.year < YEAR:
        end_month = 1
    else:
        end_month = today.month
    end_month = max(1, min(end_month, 12))
    return [(m, _MONTH_NAMES[m - 1]) for m in range(1, end_month + 1)]


def _polygon_coords_for_kml(geom) -> List[List[List[float]]]:
    out: List[List[List[float]]] = []
    if isinstance(geom, Polygon):
        out.append([[float(x), float(y)] for x, y in geom.exterior.coords])
    elif isinstance(geom, MultiPolygon):
        for poly in geom.geoms:
            out.append([[float(x), float(y)] for x, y in poly.exterior.coords])
    return out


def silt_geometry(kml_bytes: bytes) -> Tuple[ee.Geometry, Dict[str, Any]]:
    """AOI from KML only — no buffer."""
    geom = load_geometry_from_kml_bytes(kml_bytes)
    clon, clat = float(geom.centroid.x), float(geom.centroid.y)
    aoi_ee = _geom_to_ee(geom)
    meta = {
        "centroid": {"latitude": round(clat, 6), "longitude": round(clon, 6)},
        "input_line_coords": _line_coords_for_kml(geom),
        "input_polygon_coords": _polygon_coords_for_kml(geom),
    }
    return aoi_ee, meta


def _mask_s2(image: ee.Image) -> ee.Image:
    """SCL cloud mask; scale reflectance bands only (not SCL)."""
    scl = image.select("SCL")
    cloud_mask = (
        scl.neq(3)
        .And(scl.neq(8))
        .And(scl.neq(9))
        .And(scl.neq(10))
        .And(scl.neq(11))
    )
    reflectance = image.select(["B2", "B3", "B4", "B8", "B11", "B12"]).divide(10000)
    return (
        reflectance.addBands(scl)
        .updateMask(cloud_mask)
        .copyProperties(image, image.propertyNames())
    )


def _safe_divide(a: ee.Image, b: ee.Image) -> ee.Image:
    return a.divide(b.abs().max(0.0001))


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
    for candidate in (DISPLAY_SCALE_M, *DISPLAY_SCALE_FALLBACK, float(SCALE)):
        if _estimate_pixel_count(box, candidate) <= MAX_EXPORT_PIXELS:
            return candidate
    ratio = math.sqrt(_estimate_pixel_count(box, SCALE) / MAX_EXPORT_PIXELS)
    return round(SCALE * ratio, 1)


def _smooth_classified(image: ee.Image) -> ee.Image:
    first = image.focal_mode(radius=SMOOTH_RADIUS_M, units="meters")
    return first.focal_mode(radius=max(SMOOTH_RADIUS_M // 2, 12), units="meters")


def _smooth_water_mask(mask: ee.Image) -> ee.Image:
    return mask.focal_max(radius=MASK_SMOOTH_M, units="meters").focal_min(
        radius=MASK_SMOOTH_M, units="meters"
    )


def _pct(part: float, whole: float) -> float:
    if whole <= 0:
        return 0.0
    return round(part / whole * 100.0, 1)


def _create_water_mask(image: ee.Image, region: ee.Geometry) -> ee.Image:
    """Water from Sentinel-2 only (SCL + NDWI/MNDWI) — no Dynamic World."""
    green = image.select("B3")
    nir = image.select("B8")
    swir1 = image.select("B11")

    ndwi = _safe_divide(green.subtract(nir), green.add(nir))
    mndwi = _safe_divide(green.subtract(swir1), green.add(swir1))

    scl = image.select("SCL")
    scl_water = scl.eq(6)
    spectral_water = mndwi.gt(-0.10).And(ndwi.gt(-0.10))

    water = scl_water.Or(spectral_water)
    water = water.clip(region)

    connected = water.selfMask().connectedPixelCount(100, True)
    water = water.updateMask(connected.gte(4))
    water = water.focal_max(radius=1, units="pixels").focal_min(
        radius=1, units="pixels"
    )
    return water.selfMask().rename("Water")


def _area_m2(mask: ee.Image, geometry: ee.Geometry) -> float:
    result = (
        ee.Image.pixelArea()
        .updateMask(mask)
        .reduceRegion(
            reducer=ee.Reducer.sum(),
            geometry=geometry,
            scale=SCALE,
            maxPixels=MAX_PIXELS,
            bestEffort=True,
            tileScale=TILE_SCALE,
        )
        .getInfo()
        or {}
    )
    return float(result.get("area", 0) or 0)


def _silt_vis_image(
    silt_class: ee.Image, water: ee.Image, aoi: ee.Geometry
) -> ee.Image:
    """Smoothed class overlay on water only — no square borders."""
    export_scale = _export_scale_for_geometry(aoi)
    clipped = silt_class.clip(aoi)
    water_smooth = _smooth_water_mask(water.clip(aoi))
    smoothed = _smooth_classified(clipped)
    masked = smoothed.updateMask(water_smooth).clip(aoi)
    scaled = masked.reproject(crs="EPSG:4326", scale=export_scale)
    return scaled.visualize(min=1, max=4, palette=CLASS_PALETTE).updateMask(
        scaled.mask()
    )


def _volume_pixel_area() -> ee.Image:
    return ee.Image.pixelArea().reproject(
        crs="EPSG:4326", scale=VOLUME_PIXEL_SCALE_M
    )


def _volume_color_max(aoi: ee.Geometry) -> float:
    """Shared colour-scale max = largest possible pixel volume (score = 1)."""
    stats = _volume_pixel_area().reduceRegion(
        reducer=ee.Reducer.max(),
        geometry=aoi,
        scale=VOLUME_PIXEL_SCALE_M,
        maxPixels=MAX_PIXELS,
        bestEffort=True,
        tileScale=TILE_SCALE,
    ).getInfo() or {}
    area = float(stats.get("area", 0) or 0) or float(VOLUME_PIXEL_SCALE_M**2)
    return round(area * SILT_LAYER_THICKNESS_M, 2)


def _silt_volume_vis_image(
    silt_volume: ee.Image, water: ee.Image, aoi: ee.Geometry, vmax: float
) -> ee.Image:
    """Continuous volume surface on water only (YlOrBr, fixed 0–vmax scale)."""
    export_scale = _export_scale_for_geometry(aoi)
    water_smooth = _smooth_water_mask(water.clip(aoi))
    masked = silt_volume.clip(aoi).updateMask(water_smooth)
    scaled = masked.reproject(crs="EPSG:4326", scale=export_scale)
    return scaled.visualize(min=0, max=vmax, palette=VOLUME_PALETTE).updateMask(
        scaled.mask()
    )


def _export_overlay_png(vis_image: ee.Image, geometry: ee.Geometry) -> bytes:
    export_scale = _export_scale_for_geometry(geometry)
    download_params: Dict[str, Any] = {
        "region": geometry,
        "scale": export_scale,
        "crs": "EPSG:4326",
        "format": "PNG",
    }
    try:
        url = vis_image.getDownloadURL(download_params)
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
    legend_lines: List[str],
) -> bytes:
    """GroundOverlay only — no placemark borders."""
    root = ET.Element(f"{{{KML_NS}}}kml")
    doc = _kml_el(root, "Document")
    _kml_el(doc, "name", title)
    _kml_el(doc, "description", description + "\n\n" + "\n".join(legend_lines))

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


def _process_month(
    aoi: ee.Geometry,
    month_number: int,
    month_name: str,
    *,
    box: Dict[str, float],
    year: int,
    end_day_exclusive: Optional[ee.Date] = None,
    volume_vmax: Optional[float] = None,
) -> Optional[Dict[str, Any]]:
    start_date = ee.Date.fromYMD(year, month_number, 1)
    end_date = start_date.advance(1, "month")
    if end_day_exclusive is not None:
        end_date = ee.Date(
            ee.Algorithms.If(
                end_date.millis().lt(end_day_exclusive.millis()),
                end_date,
                end_day_exclusive,
            )
        )

    raw_s2 = (
        ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
        .filterBounds(aoi)
        .filterDate(start_date, end_date)
        .filter(ee.Filter.lte("CLOUDY_PIXEL_PERCENTAGE", MAX_CLOUD_S2))
    )
    s2_count = int(raw_s2.size().getInfo() or 0)
    if s2_count == 0:
        return None

    image = raw_s2.map(_mask_s2).median().clip(aoi)
    water = _create_water_mask(image, aoi)

    blue = image.select("B2")
    red = image.select("B4")

    sediment_index = _safe_divide(red.subtract(blue), red.add(blue)).rename(
        "Sediment_Index"
    )
    sediment_water = sediment_index.updateMask(water)

    percentile_stats = sediment_water.reduceRegion(
        reducer=ee.Reducer.percentile([5, 95]),
        geometry=aoi,
        scale=SCALE,
        maxPixels=MAX_PIXELS,
        bestEffort=True,
        tileScale=TILE_SCALE,
    ).getInfo() or {}

    p5 = float(percentile_stats.get("Sediment_Index_p5", 0) or 0)
    p95 = float(percentile_stats.get("Sediment_Index_p95", 1) or 1)
    score_range = max(p95 - p5, 0.0001)

    silt_score = (
        sediment_water.subtract(p5).divide(score_range).clamp(0, 1).rename("Silt_Score")
    )
    silt_class = (
        silt_score.expression(
            "(s <= 0.25) ? 1 : (s <= 0.50) ? 2 : (s <= 0.75) ? 3 : 4",
            {"s": silt_score},
        )
        .rename("Silt_Class")
        .updateMask(water)
    )

    water_m2 = _area_m2(water, aoi)
    water_ha = round(water_m2 / 10000.0, 2)

    categories = []
    for cls in SILT_CLASSES:
        cid = int(cls["id"])
        ha = round(_area_m2(silt_class.eq(cid), aoi) / 10000.0, 2)
        categories.append(
            {
                "id": cid,
                "name": cls["name"],
                "range": cls["range"],
                "color": cls["color"],
                "area_ha": ha,
                "percent": _pct(ha, water_ha),
            }
        )

    mean_dict = silt_score.reduceRegion(
        reducer=ee.Reducer.mean(),
        geometry=aoi,
        scale=SCALE,
        maxPixels=MAX_PIXELS,
        bestEffort=True,
        tileScale=TILE_SCALE,
    ).getInfo() or {}
    mean_silt = float(mean_dict.get("Silt_Score", 0) or 0)

    vis = _silt_vis_image(silt_class, water, aoi)
    png = _export_overlay_png(vis, aoi)
    legend = [f"{c['color']}  {c['id']} = {c['name']}" for c in SILT_CLASSES]
    month_code = f"{month_number:02d}"

    kml_bytes = _build_kml(
        title=f"Silt Classification — {month_name} {year}",
        description=(
            f"Monthly silt · {month_name} {year}\n"
            f"Sentinel-2 median (SCL + spectral water)\n"
            f"Water area: {water_ha} ha · Mean silt score: {round(mean_silt, 4)}\n"
            f"Sediment index P5={round(p5, 4)} · P95={round(p95, 4)}\n"
            "Class % of water area"
        ),
        box=box,
        overlay_name=f"{month_name} {year} Silt",
        png_bytes=png,
        legend_lines=["SILT CLASSIFICATION", *legend],
    )

    # ---- Silt volume surface (new layer; classification above unchanged) ----
    vmax = volume_vmax or float(VOLUME_PIXEL_SCALE_M**2) * SILT_LAYER_THICKNESS_M
    silt_volume = (
        silt_score.multiply(_volume_pixel_area())
        .multiply(SILT_LAYER_THICKNESS_M)
        .rename("Silt_Volume")
    )
    volume_total = silt_score.multiply(ee.Image.pixelArea()).multiply(
        SILT_LAYER_THICKNESS_M
    ).rename("Silt_Volume_Total").reduceRegion(
        reducer=ee.Reducer.sum(),
        geometry=aoi,
        scale=SCALE,
        maxPixels=MAX_PIXELS,
        bestEffort=True,
        tileScale=TILE_SCALE,
    ).getInfo() or {}
    total_volume_m3 = float(volume_total.get("Silt_Volume_Total", 0) or 0)
    volume_png = _export_overlay_png(
        _silt_volume_vis_image(silt_volume, water, aoi, vmax), aoi
    )
    volume_kml_filename = (
        f"silt_volume_{year}_{month_code}_{month_name.lower()}.kml"
    )
    volume_kml_bytes = _build_kml(
        title=f"Silt Volume Surface — {month_name} {year}",
        description=(
            f"Silt volume surface · {month_name} {year}\n"
            f"Volume per {VOLUME_PIXEL_SCALE_M} m pixel = silt score × pixel area × "
            f"{SILT_LAYER_THICKNESS_M} m assumed layer thickness\n"
            f"Colour scale fixed 0–{vmax} (shared across all months)\n"
            f"Total relative silt volume: {round(total_volume_m3, 1)} m³\n"
            "Relative index volume — not surveyed sediment volume."
        ),
        box=box,
        overlay_name=f"{month_name} {year} Silt Volume",
        png_bytes=volume_png,
        legend_lines=[
            "SILT VOLUME SURFACE (YlOrBr)",
            "#FFFFE5  0.00",
            f"#FE9929  {round(vmax / 2, 2)}",
            f"#662506  {vmax}",
        ],
    )

    return {
        "month": month_name,
        "month_number": month_number,
        "year": year,
        "month_code": month_code,
        "sentinel2_image_count": s2_count,
        "water_area_ha": water_ha,
        "water_area_km2": round(water_m2 / 1_000_000.0, 4),
        "mean_silt_score": round(mean_silt, 4),
        "p5": round(p5, 4),
        "p95": round(p95, 4),
        "percent_basis": "water_area",
        "categories": categories,
        "legend": SILT_CLASSES,
        "export_scale_m": _export_scale_for_geometry(aoi),
        "kml_bytes": kml_bytes,
        "kml_filename": f"silt_{year}_{month_code}_{month_name.lower()}.kml",
        "silt_volume": {
            "total_volume_m3": round(total_volume_m3, 2),
            "pixel_scale_m": VOLUME_PIXEL_SCALE_M,
            "assumed_layer_thickness_m": SILT_LAYER_THICKNESS_M,
            "color_scale": {"min": 0.0, "max": vmax},
            "palette": VOLUME_PALETTE,
            "kml_filename": volume_kml_filename,
        },
        "volume_kml_bytes": volume_kml_bytes,
    }


def analyze_silt(
    aoi_geometry: ee.Geometry,
    *,
    aoi_info: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Run silt classification for January → today's month in YEAR.

    Months without Sentinel-2 images are skipped (no hard failure).
    """
    today = date.today()
    months = active_months(today)
    end_exclusive = ee.Date.fromYMD(today.year, today.month, today.day).advance(
        1, "day"
    )

    analysis_area_ha = round(
        float(aoi_geometry.area(1).divide(10000).getInfo()), 2
    )
    box = _aoi_box(aoi_geometry)
    volume_vmax = _volume_color_max(aoi_geometry)

    monthly: Dict[str, Dict[str, Any]] = {}
    skipped: List[str] = []

    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {
            pool.submit(
                _process_month,
                aoi_geometry,
                num,
                name,
                box=box,
                year=YEAR,
                end_day_exclusive=(
                    end_exclusive
                    if num == today.month and today.year == YEAR
                    else None
                ),
                volume_vmax=volume_vmax,
            ): name
            for num, name in months
        }
        for future in as_completed(futures):
            month_name = futures[future]
            layer = future.result()
            if layer is None:
                skipped.append(month_name)
            else:
                monthly[month_name.lower()] = layer

    if not monthly:
        raise ValueError(
            f"No Sentinel-2 scenes found for January–{_MONTH_NAMES[months[-1][0] - 1]} "
            f"{YEAR} over the KML area."
        )

    months_out: Dict[str, Any] = {}
    combined_rows: List[Dict[str, Any]] = []
    for num, name in months:
        key = name.lower()
        if key not in monthly:
            continue
        layer = monthly[key]
        months_out[key] = {
            k: v
            for k, v in layer.items()
            if k not in ("kml_bytes", "volume_kml_bytes")
        }
        combined_rows.append(
            {
                "year": YEAR,
                "month": name,
                "month_number": num,
                "water_area_ha": layer["water_area_ha"],
                "mean_silt_score": layer["mean_silt_score"],
                "total_silt_volume_m3": layer["silt_volume"]["total_volume_m3"],
                "categories": layer["categories"],
            }
        )

    last_name = months[-1][1]
    result: Dict[str, Any] = {
        "year": YEAR,
        "period": f"January {YEAR} – {last_name} {YEAR} (through {today.isoformat()})",
        "analysis_area_ha": analysis_area_ha,
        "resolution_m": SCALE,
        "percent_basis": "water_area",
        "legend": SILT_CLASSES,
        "months": months_out,
        "combined_summary": combined_rows,
        "skipped_months_no_imagery": skipped,
        "notes": {
            "no_buffer": "Analysis clipped to uploaded KML geometry only.",
            "dynamic_world": "Removed — water from Sentinel-2 SCL + NDWI/MNDWI only.",
            "smoothing": (
                "Class edges use focal_mode + fine export grid for smooth pixels "
                "without square borders."
            ),
            "method": (
                "Sediment index (Red-Blue)/(Red+Blue) normalised by P5–P95 on water; "
                "classes by silt score quartiles. Percent of water area."
            ),
        },
    }

    for key, layer in monthly.items():
        result[f"{key}_kml_bytes"] = layer["kml_bytes"]
        result[f"{key}_volume_kml_bytes"] = layer["volume_kml_bytes"]

    if aoi_info:
        result["aoi"] = aoi_info
    return result
