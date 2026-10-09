"""
jalnetra.salinity_service — Relative salinity index from Sentinel-2 + Dynamic World.

Python port of the Mula-Mutha relative salinity GEE workflow.
"""
from __future__ import annotations

import base64
import math
import xml.etree.ElementTree as ET
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
ANALYSIS_SCALE = 10
DISPLAY_SCALE_M = 5
MAX_EXPORT_PIXELS = 12_000_000
MAX_CLOUD_S2 = 40
DW_WATER_THRESHOLD = 0.30

SALINITY_PALETTE = [
    "0000FF",
    "00BFFF",
    "00FFFF",
    "00FF00",
    "FFFF00",
    "FF8800",
    "FF0000",
]

SALINITY_CLASSES: List[Dict[str, Any]] = [
    {
        "id": 1,
        "name": "Very Low",
        "range": "0.00-0.20",
        "min": 0.0,
        "max": 0.20,
        "color": "#0000FF",
    },
    {
        "id": 2,
        "name": "Low",
        "range": "0.20-0.40",
        "min": 0.20,
        "max": 0.40,
        "color": "#00BFFF",
    },
    {
        "id": 3,
        "name": "Moderate",
        "range": "0.40-0.60",
        "min": 0.40,
        "max": 0.60,
        "color": "#00FF00",
    },
    {
        "id": 4,
        "name": "High",
        "range": "0.60-0.80",
        "min": 0.60,
        "max": 0.80,
        "color": "#FFFF00",
    },
    {
        "id": 5,
        "name": "Very High",
        "range": "0.80-1.00",
        "min": 0.80,
        "max": 1.00,
        "color": "#FF0000",
    },
]


def _polygon_coords_for_kml(geom) -> List[List[List[float]]]:
    out: List[List[List[float]]] = []
    if isinstance(geom, Polygon):
        out.append([[float(x), float(y)] for x, y in geom.exterior.coords])
    elif isinstance(geom, MultiPolygon):
        for poly in geom.geoms:
            out.append([[float(x), float(y)] for x, y in poly.exterior.coords])
    return out


def salinity_geometry(kml_bytes: bytes) -> Tuple[ee.Geometry, Dict[str, Any]]:
    """AOI clipped to input KML geometry (no buffer)."""
    geom = load_geometry_from_kml_bytes(kml_bytes)
    clon, clat = float(geom.centroid.x), float(geom.centroid.y)
    aoi_ee = _geom_to_ee(geom)
    meta = {
        "centroid": {"latitude": round(clat, 6), "longitude": round(clon, 6)},
        "input_line_coords": _line_coords_for_kml(geom),
        "input_polygon_coords": _polygon_coords_for_kml(geom),
    }
    return aoi_ee, meta


def _mask_and_scale_s2(image: ee.Image) -> ee.Image:
    qa = image.select("QA60")
    cloud_bit_mask = 1 << 10
    cirrus_bit_mask = 1 << 11
    mask = (
        qa.bitwiseAnd(cloud_bit_mask)
        .eq(0)
        .And(qa.bitwiseAnd(cirrus_bit_mask).eq(0))
    )
    return image.updateMask(mask).divide(10000).copyProperties(
        image, image.propertyNames()
    )


def _salinity_normalize(
    image: ee.Image, band_name: str, aoi_geometry: ee.Geometry
) -> ee.Image:
    stats = image.reduceRegion(
        reducer=ee.Reducer.percentile([5, 95]),
        geometry=aoi_geometry,
        scale=ANALYSIS_SCALE,
        maxPixels=1e10,
        bestEffort=True,
    )
    min_value = ee.Number(stats.get(f"{band_name}_p5"))
    max_value = ee.Number(stats.get(f"{band_name}_p95"))
    return image.subtract(min_value).divide(
        max_value.subtract(min_value).max(0.000001)
    ).clamp(0, 1)


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
    for candidate in (DISPLAY_SCALE_M, float(ANALYSIS_SCALE)):
        if _estimate_pixel_count(box, candidate) <= MAX_EXPORT_PIXELS:
            return candidate
    ratio = math.sqrt(_estimate_pixel_count(box, ANALYSIS_SCALE) / MAX_EXPORT_PIXELS)
    return round(ANALYSIS_SCALE * ratio, 1)


def _pct(part: float, whole: float) -> float:
    if whole <= 0:
        return 0.0
    return round(part / whole * 100.0, 1)


def _masked_area_ha(mask: ee.Image, geometry: ee.Geometry) -> float:
    area_img = (
        ee.Image.pixelArea()
        .divide(10000)
        .updateMask(mask)
        .rename("area_ha")
    )
    result = area_img.reduceRegion(
        reducer=ee.Reducer.sum(),
        geometry=geometry,
        scale=ANALYSIS_SCALE,
        maxPixels=1e13,
        bestEffort=True,
        tileScale=4,
    )
    value = ee.Number(
        ee.Algorithms.If(result.contains("area_ha"), result.get("area_ha"), 0)
    ).getInfo()
    return round(float(value), 2)


def _range_area_ha(
    salinity: ee.Image,
    water_mask: ee.Image,
    lo: float,
    hi: float,
    geometry: ee.Geometry,
    *,
    include_hi: bool = False,
) -> float:
    if include_hi:
        range_mask = salinity.gte(lo).And(salinity.lte(hi))
    else:
        range_mask = salinity.gte(lo).And(salinity.lt(hi))
    mask = range_mask.And(water_mask)
    return _masked_area_ha(mask, geometry)


def _salinity_vis_image(
    relative_salinity: ee.Image, aoi_geometry: ee.Geometry
) -> ee.Image:
    masked = relative_salinity.clip(aoi_geometry)
    return masked.visualize(min=0, max=1, palette=SALINITY_PALETTE).updateMask(
        masked.mask()
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
    input_line_coords: Optional[List[List[List[float]]]] = None,
    input_polygon_coords: Optional[List[List[List[float]]]] = None,
) -> bytes:
    root = ET.Element(f"{{{KML_NS}}}kml")
    doc = _kml_el(root, "Document")
    _kml_el(doc, "name", title)
    _kml_el(doc, "description", description + "\n\n" + "\n".join(legend_lines))

    overlay = _kml_el(doc, "GroundOverlay")
    _kml_el(overlay, "name", overlay_name)
    icon = _kml_el(overlay, "Icon")
    b64 = base64.b64encode(png_bytes).decode("ascii")
    _kml_el(icon, "href", f"data:image/png;base64,{b64}")
    llb = _kml_el(overlay, "LatLonBox")
    _kml_el(llb, "north", f"{box['north']:.8f}")
    _kml_el(llb, "south", f"{box['south']:.8f}")
    _kml_el(llb, "east", f"{box['east']:.8f}")
    _kml_el(llb, "west", f"{box['west']:.8f}")

    polys = input_polygon_coords or []
    if polys:
        for idx, coords in enumerate(polys):
            pm_in = _kml_el(doc, "Placemark")
            _kml_el(
                pm_in, "name", "Input KML" if idx == 0 else f"Input KML {idx + 1}"
            )
            in_poly = _kml_el(pm_in, "Polygon")
            in_outer = _kml_el(in_poly, "outerBoundaryIs")
            in_lr = _kml_el(in_outer, "LinearRing")
            _kml_el(
                in_lr,
                "coordinates",
                " ".join(f"{lon},{lat},0" for lon, lat in coords),
            )
            in_style = _kml_el(pm_in, "Style")
            in_ls = _kml_el(in_style, "LineStyle")
            _kml_el(in_ls, "color", "ff00ffff")
            _kml_el(in_ls, "width", "3")
            in_ps = _kml_el(in_style, "PolyStyle")
            _kml_el(in_ps, "color", "00000000")
            _kml_el(in_ps, "fill", "0")
            _kml_el(in_ps, "outline", "1")
    elif input_line_coords:
        for idx, coords in enumerate(input_line_coords):
            pm_in = _kml_el(doc, "Placemark")
            _kml_el(
                pm_in, "name", "Input KML" if idx == 0 else f"Input KML {idx + 1}"
            )
            ls_geom = _kml_el(pm_in, "LineString")
            _kml_el(
                ls_geom,
                "coordinates",
                " ".join(f"{lon},{lat},0" for lon, lat in coords),
            )
            in_style = _kml_el(pm_in, "Style")
            in_ls = _kml_el(in_style, "LineStyle")
            _kml_el(in_ls, "color", "ff00ffff")
            _kml_el(in_ls, "width", "4")

    xml_bytes = ET.tostring(root, encoding="utf-8")
    return minidom.parseString(xml_bytes).toprettyxml(indent="  ", encoding="utf-8")


def analyze_salinity(
    aoi_geometry: ee.Geometry,
    start_date: str,
    end_date: str,
    *,
    aoi_info: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Compute relative salinity index over water pixels in the AOI."""
    aoi_mask = ee.Image.constant(1).clip(aoi_geometry).selfMask()

    s2_collection = (
        ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
        .filterBounds(aoi_geometry)
        .filterDate(start_date, end_date)
        .filter(ee.Filter.lte("CLOUDY_PIXEL_PERCENTAGE", MAX_CLOUD_S2))
        .map(_mask_and_scale_s2)
        .sort("system:time_start", False)
    )
    s2_count = int(s2_collection.size().getInfo() or 0)
    if s2_count == 0:
        raise ValueError(
            f"No Sentinel-2 scenes found between {start_date} and {end_date} "
            "over the KML area. Try a wider date range."
        )

    latest_s2 = ee.Image(s2_collection.first())
    s2 = latest_s2.clip(aoi_geometry)

    s2_date_ms = latest_s2.get("system:time_start").getInfo()
    s2_date = ee.Date(s2_date_ms).format("YYYY-MM-dd HH:mm").getInfo()
    s2_product_id = latest_s2.get("PRODUCT_ID").getInfo()
    s2_cloud_pct = latest_s2.get("CLOUDY_PIXEL_PERCENTAGE").getInfo()

    ndwi = s2.normalizedDifference(["B3", "B8"]).rename("NDWI").clip(aoi_geometry)

    dw_collection = (
        ee.ImageCollection("GOOGLE/DYNAMICWORLD/V1")
        .filterBounds(aoi_geometry)
        .filterDate(start_date, end_date)
        .sort("system:time_start", False)
    )
    dw_count = int(dw_collection.size().getInfo() or 0)
    if dw_count == 0:
        raise ValueError(
            f"No Dynamic World images found between {start_date} and {end_date} "
            "over the KML area."
        )

    latest_dw = ee.Image(dw_collection.first())
    dw_water = latest_dw.select("water").rename("DW_Water_Probability").clip(aoi_geometry)
    dw_water_mask = dw_water.gte(DW_WATER_THRESHOLD).selfMask().rename("DW_Water_Mask")

    nir = s2.select("B8")
    swir1 = s2.select("B11")
    ndwi_water = ndwi.gt(0.00)
    ndwi_strong_water = ndwi.gt(0.05)
    low_nir = nir.lt(0.35)
    low_swir = swir1.lt(0.30)

    screened_water = ndwi_water.And(low_nir).And(low_swir)
    weak_water_s2 = (
        screened_water.And(ndwi.lte(0.05))
        .selfMask()
        .rename("Weak_Screened_Water_S2")
        .clip(aoi_geometry)
    )
    strong_water_s2 = ndwi_strong_water.selfMask().rename("Strong_Water_S2").clip(aoi_geometry)

    final_water_boolean = (
        dw_water_mask.unmask(0)
        .Or(weak_water_s2.unmask(0))
        .Or(strong_water_s2.unmask(0))
    )
    water_mask = (
        final_water_boolean.selfMask()
        .rename("Water_Mask")
        .updateMask(aoi_mask)
        .clip(aoi_geometry)
    )

    sal_b2 = s2.select("B2").updateMask(water_mask).rename("SAL_B2")
    sal_b3 = s2.select("B3").updateMask(water_mask).rename("SAL_B3")
    sal_b4 = s2.select("B4").updateMask(water_mask).rename("SAL_B4")
    sal_b8 = s2.select("B8").updateMask(water_mask).rename("SAL_B8")
    sal_b11 = s2.select("B11").updateMask(water_mask).rename("SAL_B11")

    sal_red_green = sal_b4.divide(sal_b3.max(0.0001)).rename("SAL_RedGreen")
    sal_red_nir = sal_b4.divide(sal_b8.max(0.0001)).rename("SAL_RedNIR")
    sal_swir_green = sal_b11.divide(sal_b3.max(0.0001)).rename("SAL_SWIRGreen")

    sal_b2_norm = _salinity_normalize(sal_b2, "SAL_B2", aoi_geometry)
    sal_b3_norm = _salinity_normalize(sal_b3, "SAL_B3", aoi_geometry)
    sal_b4_norm = _salinity_normalize(sal_b4, "SAL_B4", aoi_geometry)
    sal_red_green_norm = _salinity_normalize(sal_red_green, "SAL_RedGreen", aoi_geometry)
    sal_red_nir_norm = _salinity_normalize(sal_red_nir, "SAL_RedNIR", aoi_geometry)
    sal_swir_green_norm = _salinity_normalize(
        sal_swir_green, "SAL_SWIRGreen", aoi_geometry
    )

    ndci_water = (
        s2.normalizedDifference(["B5", "B4"])
        .rename("NDCI_Water")
        .updateMask(water_mask)
        .clip(aoi_geometry)
    )
    sal_ndci_norm = _salinity_normalize(ndci_water, "NDCI_Water", aoi_geometry)

    relative_salinity = (
        sal_b2_norm.multiply(0.20)
        .add(sal_b3_norm.multiply(0.15))
        .add(sal_b4_norm.multiply(0.20))
        .add(sal_red_green_norm.multiply(0.15))
        .add(sal_red_nir_norm.multiply(0.15))
        .add(sal_swir_green_norm.multiply(0.10))
        .add(sal_ndci_norm.multiply(0.05))
        .clamp(0, 1)
        .rename("Relative_Salinity_Index")
        .updateMask(water_mask)
        .clip(aoi_geometry)
    )

    salinity_stats = relative_salinity.reduceRegion(
        reducer=ee.Reducer.count()
        .combine(reducer2=ee.Reducer.mean(), sharedInputs=True)
        .combine(reducer2=ee.Reducer.minMax(), sharedInputs=True)
        .combine(reducer2=ee.Reducer.stdDev(), sharedInputs=True),
        geometry=aoi_geometry,
        scale=ANALYSIS_SCALE,
        maxPixels=1e10,
        bestEffort=True,
    )
    stats_info = salinity_stats.getInfo() or {}

    min_salinity = round(float(stats_info.get("Relative_Salinity_Index_min", 0) or 0), 4)
    mean_salinity = round(float(stats_info.get("Relative_Salinity_Index_mean", 0) or 0), 4)
    max_salinity = round(float(stats_info.get("Relative_Salinity_Index_max", 0) or 0), 4)
    std_salinity = round(float(stats_info.get("Relative_Salinity_Index_stdDev", 0) or 0), 4)
    valid_pixels = int(stats_info.get("Relative_Salinity_Index_count", 0) or 0)

    total_water_ha = _masked_area_ha(water_mask, aoi_geometry)
    analysis_area_ha = round(float(aoi_geometry.area(1).divide(10000).getInfo()), 2)

    categories = []
    for idx, cls in enumerate(SALINITY_CLASSES):
        lo = float(cls["min"])
        hi = float(cls["max"])
        include_hi = idx == len(SALINITY_CLASSES) - 1
        ha = _range_area_ha(
            relative_salinity,
            water_mask,
            lo,
            hi,
            aoi_geometry,
            include_hi=include_hi,
        )
        categories.append(
            {
                "id": cls["id"],
                "status": cls["name"],
                "range": cls["range"],
                "color": cls["color"],
                "area_ha": ha,
                "percent": _pct(ha, total_water_ha),
            }
        )

    vis = _salinity_vis_image(relative_salinity, aoi_geometry)
    export_scale_m = _export_scale_for_geometry(aoi_geometry)
    png = _export_overlay_png(vis, aoi_geometry)
    box = _aoi_box(aoi_geometry)
    legend = [f"{c['color']}  {c['range']}  {c['name']}" for c in SALINITY_CLASSES]

    input_lines = None
    input_polys = None
    if aoi_info:
        input_lines = aoi_info.get("input_line_coords")
        input_polys = aoi_info.get("input_polygon_coords")

    kml_bytes = _build_kml(
        title="Relative Salinity Index",
        description=(
            f"Relative salinity (0–1) · {start_date} to {end_date}\n"
            f"Latest Sentinel-2: {s2_date}\n"
            f"Mean salinity: {mean_salinity}\n"
            f"Water area: {total_water_ha} ha\n"
            "NOTE: Relative index only — not PSU, ppt, mg/L or dS/m."
        ),
        box=box,
        overlay_name="Relative Salinity",
        png_bytes=png,
        legend_lines=["RELATIVE SALINITY", *legend],
        input_line_coords=input_lines,
        input_polygon_coords=input_polys,
    )

    result: Dict[str, Any] = {
        "start_date": start_date,
        "end_date": end_date,
        "sentinel2_image_count": s2_count,
        "dynamic_world_image_count": dw_count,
        "latest_sentinel2_date": s2_date,
        "latest_sentinel2_product_id": s2_product_id,
        "latest_sentinel2_cloud_percentage": s2_cloud_pct,
        "analysis_area_ha": analysis_area_ha,
        "total_water_area_ha": total_water_ha,
        "valid_salinity_pixels": valid_pixels,
        "min_relative_salinity": min_salinity,
        "mean_relative_salinity": mean_salinity,
        "max_relative_salinity": max_salinity,
        "std_relative_salinity": std_salinity,
        "dw_water_threshold": DW_WATER_THRESHOLD,
        "resolution_m": ANALYSIS_SCALE,
        "index_range": "0-1",
        "disclaimer": (
            "Relative salinity is a satellite-derived relative index. "
            "It is NOT laboratory salinity in PSU, ppt, mg/L or dS/m."
        ),
        "percent_basis": "total_water_area",
        "categories": categories,
        "legend": SALINITY_CLASSES,
        "export_scale_m": export_scale_m,
        "kml_bytes": kml_bytes,
    }
    if aoi_info:
        result["aoi"] = aoi_info
    return result
