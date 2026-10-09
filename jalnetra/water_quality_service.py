"""
jalnetra.water_quality_service — Water quality via Sentinel-2 + Dynamic World + Landsat 9.

Python port of the Mula-Mutha water quality GEE workflow.
Returns four KML layers: WST, TSS, NDWI (permanent water), NDCI (chlorophyll).
"""
from __future__ import annotations

import base64
import math
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
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
SCALE_10M = 10
SCALE_30M = 30
DISPLAY_SCALE_M = 5  # fine grid — matches vegetation/salinity exports
DISPLAY_SCALE_FALLBACK = [8.0, 10.0]
SMOOTH_RADIUS_10M = 55
SMOOTH_RADIUS_30M = 85
DISPLAY_SMOOTH_M = 22
MASK_SMOOTH_M = 35
MAX_EXPORT_PIXELS = 12_000_000
MAX_CLOUD_S2 = 40
DW_WATER_THRESHOLD = 0.30

WST_CLASSES: List[Dict[str, Any]] = [
    {"id": 1, "name": "Very Low", "range": "<27 °C", "color": "#1565C0"},
    {"id": 2, "name": "Low", "range": "27–<30 °C", "color": "#64B5F6"},
    {"id": 3, "name": "Moderate", "range": "30–<33 °C", "color": "#FFD54F"},
    {"id": 4, "name": "High", "range": "33–<36 °C", "color": "#E53935"},
    {"id": 5, "name": "Very High", "range": "≥36 °C", "color": "#8E0000"},
]
TSS_CLASSES: List[Dict[str, Any]] = [
    {"id": 1, "name": "Low", "range": "TSS ≤ 0.62", "color": "#2196F3"},
    {"id": 2, "name": "Medium", "range": "0.62–1.17", "color": "#FFC107"},
    {"id": 3, "name": "High", "range": "TSS > 1.17", "color": "#F44336"},
]
NDWI_CLASSES: List[Dict[str, Any]] = [
    {"id": 1, "name": "Non-water", "range": "NDWI ≤ 0", "color": "#8D6E63"},
    {"id": 2, "name": "Water", "range": "NDWI > 0", "color": "#0D47A1"},
]
NDCI_CLASSES: List[Dict[str, Any]] = [
    {"id": 1, "name": "Low Chlorophyll", "range": "NDCI < 0", "color": "#C8E6C9"},
    {"id": 2, "name": "High Chlorophyll", "range": "NDCI ≥ 0", "color": "#1B5E20"},
]

WST_PALETTE = ["1565C0", "64B5F6", "FFD54F", "E53935", "8E0000"]
TSS_PALETTE = ["2196F3", "FFC107", "F44336"]
NDWI_PALETTE = ["8D6E63", "0D47A1"]
NDCI_PALETTE = ["C8E6C9", "1B5E20"]


def _polygon_coords_for_kml(geom) -> List[List[List[float]]]:
    out: List[List[List[float]]] = []
    if isinstance(geom, Polygon):
        out.append([[float(x), float(y)] for x, y in geom.exterior.coords])
    elif isinstance(geom, MultiPolygon):
        for poly in geom.geoms:
            out.append([[float(x), float(y)] for x, y in poly.exterior.coords])
    return out


def water_quality_geometry(kml_bytes: bytes) -> Tuple[ee.Geometry, Dict[str, Any]]:
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


def _mask_l9(image: ee.Image) -> ee.Image:
    qa_pixel = image.select("QA_PIXEL")
    qa_radsat = image.select("QA_RADSAT")
    clear_mask = (
        qa_pixel.bitwiseAnd(1 << 0)
        .eq(0)
        .And(qa_pixel.bitwiseAnd(1 << 1).eq(0))
        .And(qa_pixel.bitwiseAnd(1 << 2).eq(0))
        .And(qa_pixel.bitwiseAnd(1 << 3).eq(0))
        .And(qa_pixel.bitwiseAnd(1 << 4).eq(0))
        .And(qa_pixel.bitwiseAnd(1 << 5).eq(0))
    )
    saturation_mask = qa_radsat.eq(0)
    return image.updateMask(clear_mask).updateMask(saturation_mask).copyProperties(
        image, image.propertyNames()
    )


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


def _export_scale_for_geometry(geometry: ee.Geometry, native_scale: int) -> float:
    box = _aoi_box(geometry)
    candidates = [DISPLAY_SCALE_M, *DISPLAY_SCALE_FALLBACK, float(native_scale)]
    for candidate in candidates:
        if _estimate_pixel_count(box, candidate) <= MAX_EXPORT_PIXELS:
            return candidate
    ratio = math.sqrt(_estimate_pixel_count(box, native_scale) / MAX_EXPORT_PIXELS)
    return round(native_scale * ratio, 1)


def _smooth_classified(image: ee.Image, radius_m: int) -> ee.Image:
    """Three-pass mode filter — soft class edges without heavy block corners."""
    first = image.focal_mode(radius=radius_m, units="meters")
    second = first.focal_mode(radius=max(radius_m // 2, 12), units="meters")
    return second.focal_mode(radius=max(radius_m // 4, 8), units="meters")


def _smooth_binary_mask(mask: ee.Image) -> ee.Image:
    """Morphological close — smooth mask outline before colouring."""
    return mask.focal_max(radius=MASK_SMOOTH_M, units="meters").focal_min(
        radius=MASK_SMOOTH_M, units="meters"
    )


def _aoi_mask_image(aoi: ee.Geometry) -> ee.Image:
    return ee.Image.constant(1).clip(aoi).selfMask()


def _pct(part: float, whole: float) -> float:
    if whole <= 0:
        return 0.0
    return round(part / whole * 100.0, 1)


def _masked_area_ha(mask: ee.Image, geometry: ee.Geometry, scale: int) -> float:
    area_img = (
        ee.Image.pixelArea()
        .divide(10000)
        .updateMask(mask)
        .rename("area_ha")
    )
    result = area_img.reduceRegion(
        reducer=ee.Reducer.sum(),
        geometry=geometry,
        scale=scale,
        maxPixels=1e13,
        bestEffort=True,
    ).getInfo() or {}
    return round(float(result.get("area_ha", 0) or 0), 2)


def _class_areas_ha(
    class_image: ee.Image, geometry: ee.Geometry, scale: int
) -> Dict[int, float]:
    area_img = (
        ee.Image.pixelArea()
        .divide(10000)
        .addBands(class_image.rename("class"))
    )
    result = area_img.reduceRegion(
        reducer=ee.Reducer.sum().group(groupField=1, groupName="class"),
        geometry=geometry,
        scale=scale,
        maxPixels=1e13,
        bestEffort=True,
    ).getInfo() or {}
    areas: Dict[int, float] = {}
    for group in result.get("groups", []) or []:
        cid = int(group["class"])
        areas[cid] = round(float(group["sum"]), 2)
    return areas


def _class_vis_image(
    class_image: ee.Image,
    aoi: ee.Geometry,
    *,
    min_val: int,
    max_val: int,
    palette: List[str],
    display_mask: Optional[ee.Image] = None,
    native_scale: int = SCALE_10M,
    full_aoi: bool = False,
) -> ee.Image:
    export_scale = _export_scale_for_geometry(aoi, native_scale)
    smooth_radius = SMOOTH_RADIUS_30M if native_scale >= SCALE_30M else SMOOTH_RADIUS_10M
    aoi_mask = _aoi_mask_image(aoi)
    clipped = class_image.clip(aoi)

    if full_aoi:
        region_mask = aoi_mask
    else:
        raw_mask = display_mask if display_mask is not None else clipped.mask()
        region_mask = _smooth_binary_mask(raw_mask).updateMask(aoi_mask)

    smoothed = _smooth_classified(clipped, smooth_radius)
    masked = smoothed.updateMask(region_mask).updateMask(aoi_mask)
    scaled = masked.reproject(crs="EPSG:4326", scale=export_scale)
    at_scale = scaled.focal_mode(radius=DISPLAY_SMOOTH_M, units="meters")
    final = at_scale.focal_mode(
        radius=max(DISPLAY_SMOOTH_M // 2, 10), units="meters"
    ).updateMask(scaled.mask())
    return final.visualize(min=min_val, max=max_val, palette=palette).updateMask(
        final.mask()
    )


def _export_overlay_png(
    vis_image: ee.Image, geometry: ee.Geometry, native_scale: int
) -> bytes:
    """Scale-aligned PNG export — avoids blocky dimension-thumbnail grids."""
    export_scale = _export_scale_for_geometry(geometry, native_scale)
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


def _export_layers_parallel(
    exports: List[Tuple[str, ee.Image, ee.Geometry, int]],
) -> Dict[str, bytes]:
    """Export multiple KML overlay PNGs concurrently."""
    pngs: Dict[str, bytes] = {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {
            pool.submit(_export_overlay_png, vis, geom, scale): key
            for key, vis, geom, scale in exports
        }
        for future in as_completed(futures):
            pngs[futures[future]] = future.result()
    return pngs


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

    xml_bytes = ET.tostring(root, encoding="utf-8")
    return minidom.parseString(xml_bytes).toprettyxml(indent="  ", encoding="utf-8")


def _build_layer_result(
    *,
    layer_key: str,
    title: str,
    class_defs: List[Dict[str, Any]],
    areas: Dict[int, float],
    percent_basis: str,
    percent_whole_ha: float,
    include_percent: bool,
    png_bytes: bytes,
    aoi: ee.Geometry,
    native_scale: int,
    description: str,
) -> Dict[str, Any]:
    categories = []
    for cls in class_defs:
        cid = int(cls["id"])
        ha = areas.get(cid, 0.0)
        entry: Dict[str, Any] = {
            "id": cid,
            "name": cls["name"],
            "range": cls["range"],
            "color": cls["color"],
            "area_ha": ha,
        }
        if include_percent:
            entry["percent"] = _pct(ha, percent_whole_ha)
        categories.append(entry)

    box = _aoi_box(aoi)
    legend = [f"{c['color']}  {c['name']} ({c['range']})" for c in class_defs]
    kml_bytes = _build_kml(
        title=title,
        description=description,
        box=box,
        overlay_name=title,
        png_bytes=png_bytes,
        legend_lines=[title.upper(), *legend],
    )
    return {
        "layer": layer_key,
        "title": title,
        "percent_basis": percent_basis,
        "valid_area_ha": round(percent_whole_ha, 2) if include_percent else None,
        "categories": categories,
        "legend": class_defs,
        "export_scale_m": _export_scale_for_geometry(aoi, native_scale),
        "kml_bytes": kml_bytes,
    }


def analyze_water_quality(
    aoi_geometry: ee.Geometry,
    start_date: str,
    end_date: str,
    *,
    aoi_info: Optional[Dict[str, Any]] = None,
    landsat_window: Optional[Tuple[str, str]] = None,
) -> Dict[str, Any]:
    """Run water quality analysis and build four KML layers.

    `landsat_window` (start, end_exclusive) overrides the Landsat 9 WST date
    range; Sentinel-2 / Dynamic World always use start_date..end_date.
    """
    l9_start, l9_end = landsat_window or (start_date, end_date)
    aoi_mask = _aoi_mask_image(aoi_geometry)
    analysis_area_ha = round(
        float(aoi_geometry.area(1).divide(10000).getInfo()), 2
    )

    s2_collection = (
        ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
        .filterBounds(aoi_geometry)
        .filterDate(start_date, end_date)
        .filter(ee.Filter.lte("CLOUDY_PIXEL_PERCENTAGE", MAX_CLOUD_S2))
        .map(_mask_and_scale_s2)
        .sort("system:time_start", False)
    )
    if int(s2_collection.size().getInfo() or 0) == 0:
        raise ValueError(
            f"No Sentinel-2 scenes found between {start_date} and {end_date} "
            "over the KML area."
        )

    latest_s2 = ee.Image(s2_collection.first())
    s2 = latest_s2.clip(aoi_geometry)
    s2_date = ee.Date(latest_s2.get("system:time_start")).format(
        "YYYY-MM-dd HH:mm"
    ).getInfo()

    ndwi = s2.normalizedDifference(["B3", "B8"]).rename("NDWI").clip(aoi_geometry)
    ndci = s2.normalizedDifference(["B5", "B4"]).rename("NDCI").clip(aoi_geometry)
    tss = s2.select("B4").divide(s2.select("B8")).rename("TSS").clip(aoi_geometry)

    dw_collection = (
        ee.ImageCollection("GOOGLE/DYNAMICWORLD/V1")
        .filterBounds(aoi_geometry)
        .filterDate(start_date, end_date)
        .sort("system:time_start", False)
    )
    if int(dw_collection.size().getInfo() or 0) == 0:
        raise ValueError(
            f"No Dynamic World images found between {start_date} and {end_date}."
        )
    latest_dw = ee.Image(dw_collection.first())
    dw_date = ee.Date(latest_dw.get("system:time_start")).format(
        "YYYY-MM-dd HH:mm"
    ).getInfo()

    dw_water = latest_dw.select("water").rename("DW_Water_Probability").clip(aoi_geometry)
    dw_water_mask = (
        dw_water.gte(DW_WATER_THRESHOLD).selfMask().rename("DW_Water_Mask").clip(aoi_geometry)
    )

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

    water_area_ha = _masked_area_ha(water_mask, aoi_geometry, SCALE_10M)

    ndwi_water = ndwi.updateMask(water_mask).rename("NDWI_Water").clip(aoi_geometry)
    ndci_water = ndci.updateMask(water_mask).rename("NDCI_Water").clip(aoi_geometry)
    tss_water = tss.updateMask(water_mask).rename("TSS_Water").clip(aoi_geometry)

    l9_collection = (
        ee.ImageCollection("LANDSAT/LC09/C02/T1_L2")
        .filterBounds(aoi_geometry)
        .filterDate(l9_start, l9_end)
        .map(_mask_l9)
    )
    l9_count = int(l9_collection.size().getInfo() or 0)
    if l9_count == 0:
        raise ValueError(
            f"No Landsat 9 scenes found between {l9_start} and {l9_end} "
            "for WST analysis."
        )

    l9 = l9_collection.median().clip(aoi_geometry)
    st_b10 = l9.select("ST_B10").clip(aoi_geometry)
    wst_k = st_b10.multiply(0.00341802).add(149.0).rename("WST_K")
    wst = wst_k.subtract(273.15).rename("WST_C").clip(aoi_geometry)
    valid_temperature = wst.gte(-10).And(wst.lte(60))
    wst = wst.updateMask(valid_temperature).updateMask(aoi_mask).clip(aoi_geometry)
    wst_water = wst.updateMask(water_mask).rename("WST_Water_C").clip(aoi_geometry)

    ndwi_class = (
        ee.Image(1)
        .where(water_mask, 2)
        .rename("NDWI_Class")
        .updateMask(aoi_mask)
        .clip(aoi_geometry)
    )

    tss_class = (
        ee.Image(1)
        .where(tss_water.gt(0.62).And(tss_water.lte(1.17)), 2)
        .where(tss_water.gt(1.17), 3)
        .rename("Turbidity_Class")
        .updateMask(tss_water.mask())
        .clip(aoi_geometry)
    )

    ndci_class = (
        ee.Image(1)
        .where(ndci_water.gte(0), 2)
        .rename("NDCI_Class")
        .updateMask(ndci_water.mask())
        .clip(aoi_geometry)
    )

    wst_class = (
        ee.Image(1)
        .where(wst_water.gte(27).And(wst_water.lt(30)), 2)
        .where(wst_water.gte(30).And(wst_water.lt(33)), 3)
        .where(wst_water.gte(33).And(wst_water.lt(36)), 4)
        .where(wst_water.gte(36), 5)
        .rename("WST_Class")
        .updateMask(wst_water.mask())
        .clip(aoi_geometry)
    )

    wst_areas = _class_areas_ha(wst_class, aoi_geometry, SCALE_30M)
    tss_areas = _class_areas_ha(tss_class, aoi_geometry, SCALE_10M)
    ndwi_areas = _class_areas_ha(ndwi_class, aoi_geometry, SCALE_10M)
    ndci_areas = _class_areas_ha(ndci_class, aoi_geometry, SCALE_10M)

    wst_valid_ha = sum(wst_areas.values())
    tss_valid_ha = sum(tss_areas.values())
    ndci_valid_ha = sum(ndci_areas.values())

    base_desc = (
        f"Water quality · {start_date} to {end_date}\n"
        f"Latest Sentinel-2: {s2_date}\n"
        f"Latest Dynamic World: {dw_date}\n"
        f"Analysis area: {analysis_area_ha} ha · Water area: {water_area_ha} ha"
    )

    vis_wst = _class_vis_image(
        wst_class,
        aoi_geometry,
        min_val=1,
        max_val=5,
        palette=WST_PALETTE,
        display_mask=wst_water.mask(),
        native_scale=SCALE_30M,
    )
    vis_tss = _class_vis_image(
        tss_class,
        aoi_geometry,
        min_val=1,
        max_val=3,
        palette=TSS_PALETTE,
        display_mask=tss_water.mask(),
        native_scale=SCALE_10M,
    )
    vis_ndwi = _class_vis_image(
        ndwi_class,
        aoi_geometry,
        min_val=1,
        max_val=2,
        palette=NDWI_PALETTE,
        native_scale=SCALE_10M,
        full_aoi=True,
    )
    vis_ndci = _class_vis_image(
        ndci_class,
        aoi_geometry,
        min_val=1,
        max_val=2,
        palette=NDCI_PALETTE,
        display_mask=ndci_water.mask(),
        native_scale=SCALE_10M,
    )

    pngs = _export_layers_parallel(
        [
            ("wst", vis_wst, aoi_geometry, SCALE_30M),
            ("tss", vis_tss, aoi_geometry, SCALE_10M),
            ("ndwi", vis_ndwi, aoi_geometry, SCALE_10M),
            ("ndci", vis_ndci, aoi_geometry, SCALE_10M),
        ]
    )

    wst_layer = _build_layer_result(
        layer_key="wst",
        title="Water Surface Temperature (WST)",
        class_defs=WST_CLASSES,
        areas=wst_areas,
        percent_basis="valid_wst_water_area",
        percent_whole_ha=wst_valid_ha,
        include_percent=True,
        png_bytes=pngs["wst"],
        aoi=aoi_geometry,
        native_scale=SCALE_30M,
        description=base_desc + "\nWST source: Landsat 9 ST_B10 (°C)",
    )

    tss_layer = _build_layer_result(
        layer_key="tss",
        title="Turbidity / TSS Proxy",
        class_defs=TSS_CLASSES,
        areas=tss_areas,
        percent_basis="class_area_only",
        percent_whole_ha=tss_valid_ha,
        include_percent=False,
        png_bytes=pngs["tss"],
        aoi=aoi_geometry,
        native_scale=SCALE_10M,
        description=base_desc + "\nTSS = spectral proxy (Red/NIR), not lab mg/L",
    )

    ndwi_layer = _build_layer_result(
        layer_key="ndwi",
        title="NDWI — Permanent Water",
        class_defs=NDWI_CLASSES,
        areas=ndwi_areas,
        percent_basis="analysis_area",
        percent_whole_ha=analysis_area_ha,
        include_percent=True,
        png_bytes=pngs["ndwi"],
        aoi=aoi_geometry,
        native_scale=SCALE_10M,
        description=base_desc + "\nPermanent water classification over full AOI",
    )

    ndci_layer = _build_layer_result(
        layer_key="ndci",
        title="NDCI — Chlorophyll Proxy",
        class_defs=NDCI_CLASSES,
        areas=ndci_areas,
        percent_basis="valid_water_area",
        percent_whole_ha=ndci_valid_ha,
        include_percent=True,
        png_bytes=pngs["ndci"],
        aoi=aoi_geometry,
        native_scale=SCALE_10M,
        description=base_desc + "\nLow / High chlorophyll on water pixels only",
    )

    result: Dict[str, Any] = {
        "start_date": start_date,
        "end_date": end_date,
        "analysis_area_ha": analysis_area_ha,
        "water_area_ha": water_area_ha,
        "dw_water_threshold": DW_WATER_THRESHOLD,
        "latest_sentinel2_date": s2_date,
        "latest_dynamic_world_date": dw_date,
        "landsat9_image_count": l9_count,
        "landsat9_window": {"start": l9_start, "end": l9_end},
        "wst": {k: v for k, v in wst_layer.items() if k != "kml_bytes"},
        "tss": {k: v for k, v in tss_layer.items() if k != "kml_bytes"},
        "ndwi": {k: v for k, v in ndwi_layer.items() if k != "kml_bytes"},
        "ndci": {k: v for k, v in ndci_layer.items() if k != "kml_bytes"},
        "wst_kml_bytes": wst_layer["kml_bytes"],
        "tss_kml_bytes": tss_layer["kml_bytes"],
        "ndwi_kml_bytes": ndwi_layer["kml_bytes"],
        "ndci_kml_bytes": ndci_layer["kml_bytes"],
    }
    if aoi_info:
        result["aoi"] = aoi_info
    return result
