"""
jalnetra.vegetation_service — Urban vegetation type + health via Google Earth Engine.

Python port of the Mula-Mutha / Pune GEE workflow (Sentinel-2 + Dynamic World).
"""
from __future__ import annotations

import base64
import math
import xml.etree.ElementTree as ET
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Tuple
from xml.dom import minidom

import ee

from jalnetra.ee_http import read_url

KML_NS = "http://www.opengis.net/kml/2.2"
SCALE = 10  # Sentinel-2 native resolution (metres)
DISPLAY_SCALE_M = 5  # 2× supersample for smoother appearance when zoomed in
SMOOTH_RADIUS_M = 10  # 1-pixel majority filter at native resolution
MAX_EXPORT_PIXELS = 12_000_000
MAX_CLOUD_S2 = 40

# Map overlay palettes — vegetation pixels only (non-veg = transparent)
TYPE_PALETTE_VEG = ["006400", "8B4513", "7CFC00", "800080"]
HEALTH_PALETTE = ["006400", "32CD32", "FFFF00", "FF8C00", "FF0000"]

# Vegetation type — legend colours / names (screenshot 2)
VEG_TYPE_CLASSES: List[Dict[str, Any]] = [
    {"id": 0, "name": "Non-Vegetation", "color": "#FFFFFF"},
    {"id": 1, "name": "Trees", "color": "#006400"},
    {"id": 2, "name": "Shrub / Scrub", "color": "#8B4513"},
    {"id": 3, "name": "Grass / Herbaceous", "color": "#7CFC00"},
    {"id": 4, "name": "Mixed / Diverse", "color": "#800080"},
]

# Vegetation health — legend colours / names (screenshot 1)
VEG_HEALTH_CLASSES: List[Dict[str, Any]] = [
    {"id": 1, "name": "Very Healthy", "score_range": "80-100", "color": "#006400"},
    {"id": 2, "name": "Healthy", "score_range": "65-80", "color": "#32CD32"},
    {"id": 3, "name": "Moderate", "score_range": "50-65", "color": "#FFFF00"},
    {"id": 4, "name": "Poor", "score_range": "35-50", "color": "#FF8C00"},
    {"id": 5, "name": "Critical", "score_range": "0-35", "color": "#FF0000"},
]


def default_date_range() -> Tuple[str, str]:
    end = date.today()
    start = end - timedelta(days=31)
    return start.isoformat(), end.isoformat()


def _mask_s2(image: ee.Image) -> ee.Image:
    scl = image.select("SCL")
    mask = (
        scl.neq(3)
        .And(scl.neq(8))
        .And(scl.neq(9))
        .And(scl.neq(10))
        .And(scl.neq(11))
        .And(scl.neq(1))
    )
    return image.updateMask(mask).divide(10000).copyProperties(
        image, ["system:time_start"]
    )


def _area_ha(mask: ee.Image, geometry: ee.Geometry, scale: int = SCALE) -> float:
    area_img = ee.Image.pixelArea().updateMask(mask).rename("area_m2")
    stats = area_img.reduceRegion(
        reducer=ee.Reducer.sum(),
        geometry=geometry,
        scale=scale,
        maxPixels=1e13,
        bestEffort=True,
        tileScale=4,
    )
    sum_m2 = ee.Number(
        ee.Algorithms.If(stats.contains("area_m2"), stats.get("area_m2"), 0)
    ).getInfo()
    return round(float(sum_m2) / 10000.0, 2)


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


def _ring_from_geometry(geometry: ee.Geometry) -> List[List[float]]:
    info = geometry.bounds().getInfo()
    coords = info["coordinates"][0]
    ring = [[float(c[0]), float(c[1])] for c in coords]
    if ring[0] != ring[-1]:
        ring.append(ring[0])
    return ring


def _estimate_pixel_count(box: Dict[str, float], scale: float) -> float:
    lat_mid = (box["north"] + box["south"]) / 2.0
    width_m = (box["east"] - box["west"]) * 111320 * math.cos(math.radians(lat_mid))
    height_m = (box["north"] - box["south"]) * 110540
    return (width_m / scale) * (height_m / scale)


def _export_scale_for_geometry(geometry: ee.Geometry) -> float:
    """Pick export scale (m/px) — finer display scale when pixel budget allows."""
    box = _aoi_box(geometry)
    for candidate in (DISPLAY_SCALE_M, float(SCALE)):
        if _estimate_pixel_count(box, candidate) <= MAX_EXPORT_PIXELS:
            return candidate
    ratio = math.sqrt(_estimate_pixel_count(box, SCALE) / MAX_EXPORT_PIXELS)
    return round(SCALE * ratio, 1)


def _smooth_classified(image: ee.Image) -> ee.Image:
    """Light mode filter — softens block edges for smoother zoom rendering."""
    return image.focal_mode(radius=SMOOTH_RADIUS_M, units="meters")


def _type_vis_image(plant_type: ee.Image, analysis_area: ee.Geometry) -> ee.Image:
    """Vegetation classes only — non-vegetation pixels are transparent."""
    clipped = plant_type.clip(analysis_area)
    veg_mask = clipped.gt(0)
    masked = _smooth_classified(clipped).updateMask(veg_mask)
    return masked.visualize(min=1, max=4, palette=TYPE_PALETTE_VEG).updateMask(
        masked.mask()
    )


def _health_vis_image(
    health_class: ee.Image,
    plant_type: ee.Image,
    analysis_area: ee.Geometry,
) -> ee.Image:
    """Health classes on vegetation only — non-vegetation is transparent."""
    classified_veg = plant_type.gt(0).And(plant_type.lte(4))
    masked = (
        _smooth_classified(health_class.clip(analysis_area))
        .updateMask(classified_veg)
    )
    return masked.visualize(min=1, max=5, palette=HEALTH_PALETTE).updateMask(
        masked.mask()
    )


def _export_overlay_png(vis_image: ee.Image, geometry: ee.Geometry) -> bytes:
    """High-resolution PNG export aligned to display scale for smooth zoom."""
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


def _thumb_png(image: ee.Image, geometry: ee.Geometry, dimensions: int = 1200) -> bytes:
    """Legacy helper — prefer _export_overlay_png."""
    return _export_overlay_png(image, geometry)


def _kml_el(parent: ET.Element, tag: str, text: Optional[str] = None) -> ET.Element:
    el = ET.SubElement(parent, f"{{{KML_NS}}}{tag}")
    if text is not None:
        el.text = text
    return el


def _build_kml(
    *,
    title: str,
    description: str,
    ring: List[List[float]],
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

    # 1) Coloured vegetation layer (bottom) — transparent where no data
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

    # 2) Input KML — yellow fill for polygons, yellow line for paths
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


def _pct(part: float, whole: float) -> float:
    if whole <= 0:
        return 0.0
    return round(part / whole * 100.0, 1)


def _build_vegetation_stack(
    geometry: ee.Geometry, start_date: str, end_date: str
) -> Dict[str, Any]:
    analysis_area = geometry

    s2 = (
        ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
        .filterBounds(analysis_area)
        .filterDate(start_date, end_date)
        .filter(ee.Filter.lte("CLOUDY_PIXEL_PERCENTAGE", MAX_CLOUD_S2))
        .map(_mask_s2)
    )
    if int(s2.size().getInfo() or 0) == 0:
        raise ValueError(
            f"No Sentinel-2 scenes found between {start_date} and {end_date} "
            "over the KML area. Try a wider date range."
        )

    composite = s2.median().clip(analysis_area)

    ndvi = composite.normalizedDifference(["B8", "B4"]).rename("NDVI").clip(analysis_area)
    evi = (
        composite.expression(
            "2.5 * ((NIR - RED) / (NIR + 6 * RED - 7.5 * BLUE + 1))",
            {
                "NIR": composite.select("B8"),
                "RED": composite.select("B4"),
                "BLUE": composite.select("B2"),
            },
        )
        .rename("EVI")
        .clip(analysis_area)
    )
    ndmi = composite.normalizedDifference(["B8", "B11"]).rename("NDMI").clip(analysis_area)
    ndwi = composite.normalizedDifference(["B3", "B8"]).rename("NDWI").clip(analysis_area)
    ndbi = composite.normalizedDifference(["B11", "B8"]).rename("NDBI").clip(analysis_area)

    vegetation_mask = (
        ndvi.gte(0.25)
        .And(evi.gte(0.10))
        .And(ndwi.lt(0.40))
        .And(ndbi.lt(0.25))
        .rename("Vegetation_Mask")
        .clip(analysis_area)
    )

    dw_collection = (
        ee.ImageCollection("GOOGLE/DYNAMICWORLD/V1")
        .filterBounds(analysis_area)
        .filterDate(start_date, end_date)
    )
    dw_label = dw_collection.select("label").mode().clip(analysis_area)
    dw_probability = (
        dw_collection.select(["trees", "grass", "shrub_and_scrub", "crops"])
        .median()
        .clip(analysis_area)
    )

    trees_prob = dw_probability.select("trees").rename("Trees_Probability")
    grass_prob = dw_probability.select("grass").rename("Grass_Probability")
    shrub_prob = dw_probability.select("shrub_and_scrub").rename("Shrub_Probability")

    vegetation_probability = ee.Image.cat([trees_prob, shrub_prob, grass_prob])
    max_vegetation_probability = vegetation_probability.reduce(ee.Reducer.max())

    tree_class = (
        vegetation_mask.And(
            dw_label.eq(1).Or(trees_prob.eq(max_vegetation_probability))
        )
        .And(trees_prob.gte(0.35))
        .And(ndvi.gte(0.35))
    )
    shrub_class = (
        vegetation_mask.And(
            dw_label.eq(5).Or(shrub_prob.eq(max_vegetation_probability))
        )
        .And(shrub_prob.gte(0.25))
        .And(ndvi.gte(0.25))
    )
    grass_class = (
        vegetation_mask.And(
            dw_label.eq(2).Or(grass_prob.eq(max_vegetation_probability))
        )
        .And(grass_prob.gte(0.30))
        .And(ndvi.gte(0.25))
    )

    tree_strong = trees_prob.gte(0.25)
    shrub_strong = shrub_prob.gte(0.25)
    grass_strong = grass_prob.gte(0.25)
    number_of_strong = tree_strong.add(shrub_strong).add(grass_strong)
    mixed_vegetation = (
        vegetation_mask.And(number_of_strong.gte(2))
        .And(max_vegetation_probability.lt(0.60))
    )

    plant_type = ee.Image(0).clip(analysis_area)
    plant_type = plant_type.where(mixed_vegetation, 4)
    plant_type = plant_type.where(tree_class.And(plant_type.eq(0)), 1)
    plant_type = plant_type.where(shrub_class.And(plant_type.eq(0)), 2)
    plant_type = plant_type.where(grass_class.And(plant_type.eq(0)), 3)

    dw_tree = vegetation_mask.And(dw_label.eq(1)).And(plant_type.eq(0))
    dw_shrub = vegetation_mask.And(dw_label.eq(5)).And(plant_type.eq(0))
    dw_grass = vegetation_mask.And(dw_label.eq(2)).And(plant_type.eq(0))
    plant_type = plant_type.where(dw_tree, 1)
    plant_type = plant_type.where(dw_shrub.And(plant_type.eq(0)), 2)
    plant_type = plant_type.where(dw_grass.And(plant_type.eq(0)), 3)

    remaining_vegetation = (
        vegetation_mask.And(plant_type.eq(0))
        .And(max_vegetation_probability.gte(0.20))
    )
    plant_type = plant_type.where(
        remaining_vegetation.And(trees_prob.eq(max_vegetation_probability)), 1
    )
    plant_type = plant_type.where(
        remaining_vegetation.And(plant_type.eq(0))
        .And(shrub_prob.eq(max_vegetation_probability)),
        2,
    )
    plant_type = plant_type.where(
        remaining_vegetation.And(plant_type.eq(0))
        .And(grass_prob.eq(max_vegetation_probability)),
        3,
    )
    plant_type = plant_type.rename("Plant_Vegetation_Type").clip(analysis_area)

    classified_vegetation_mask = (
        plant_type.gte(1).And(plant_type.lte(4)).rename("Classified_Vegetation")
    )
    vegetation = classified_vegetation_mask.rename("Vegetation").clip(analysis_area)

    stressed_vegetation = (
        vegetation.And(ndvi.lt(0.45).Or(ndmi.lt(0.05)))
        .rename("Stressed_Vegetation")
        .clip(analysis_area)
    )

    ndvi_health = (
        ndvi.subtract(0.20)
        .divide(0.60)
        .multiply(100)
        .clamp(0, 100)
    )
    ndmi_health = (
        ndmi.subtract(-0.20)
        .divide(0.70)
        .multiply(100)
        .clamp(0, 100)
    )
    evi_health = (
        evi.subtract(0.10)
        .divide(0.50)
        .multiply(100)
        .clamp(0, 100)
    )
    vegetation_health = (
        ndvi_health.multiply(0.50)
        .add(ndmi_health.multiply(0.30))
        .add(evi_health.multiply(0.20))
        .rename("Vegetation_Health_Score")
        .updateMask(vegetation)
        .clip(analysis_area)
    )

    health_class = ee.Image(0).clip(analysis_area)
    health_class = health_class.where(vegetation_health.gte(80), 1)
    health_class = health_class.where(
        vegetation_health.gte(65).And(vegetation_health.lt(80)), 2
    )
    health_class = health_class.where(
        vegetation_health.gte(50).And(vegetation_health.lt(65)), 3
    )
    health_class = health_class.where(
        vegetation_health.gte(35).And(vegetation_health.lt(50)), 4
    )
    health_class = health_class.where(vegetation_health.lt(35), 5)
    health_class = (
        health_class.rename("Vegetation_Health_Class")
        .updateMask(vegetation)
        .clip(analysis_area)
    )

    return {
        "geometry": analysis_area,
        "plant_type": plant_type,
        "vegetation": vegetation,
        "vegetation_health": vegetation_health,
        "health_class": health_class,
        "stressed_vegetation": stressed_vegetation,
        "s2_count": int(s2.size().getInfo() or 0),
    }


def analyze_vegetation_type(
    geometry: ee.Geometry,
    start_date: str,
    end_date: str,
    *,
    buffer_info: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    stack = _build_vegetation_stack(geometry, start_date, end_date)
    plant_type = stack["plant_type"]
    vegetation = stack["vegetation"]
    analysis_area = stack["geometry"]

    analysis_area_ha = round(
        float(analysis_area.area(1).divide(10000).getInfo()), 2
    )

    areas: Dict[int, float] = {}
    for cls in VEG_TYPE_CLASSES:
        cid = int(cls["id"])
        areas[cid] = _area_ha(plant_type.eq(cid), analysis_area)

    total_vegetation_ha = _area_ha(vegetation, analysis_area)
    stressed_ha = _area_ha(stack["stressed_vegetation"], analysis_area)

    categories = []
    for cls in VEG_TYPE_CLASSES:
        cid = int(cls["id"])
        ha = areas[cid]
        categories.append(
            {
                "id": cid,
                "name": cls["name"],
                "color": cls["color"],
                "area_ha": ha,
                "percent": _pct(ha, analysis_area_ha),
            }
        )

    vis = _type_vis_image(plant_type, analysis_area)
    export_scale_m = _export_scale_for_geometry(analysis_area)
    png = _export_overlay_png(vis, analysis_area)
    ring = _ring_from_geometry(analysis_area)
    box = _aoi_box(analysis_area)
    legend = [f"{c['color']}  {c['name']}" for c in VEG_TYPE_CLASSES if c["id"] > 0]
    buf_desc = ""
    input_lines = None
    input_polys = None
    if buffer_info:
        buf_desc = (
            f"\nBuffer: {buffer_info['buffer_km']} km on each side\n"
            f"Analysis rectangle: {buffer_info['rectangle_width_km']} x "
            f"{buffer_info['rectangle_height_km']} km"
        )
        input_lines = buffer_info.get("input_line_coords")
        input_polys = buffer_info.get("input_polygon_coords")
    kml_bytes = _build_kml(
        title="Vegetation Type",
        description=(
            f"Urban vegetation type · {start_date} to {end_date}\n"
            f"Analysis area: {analysis_area_ha} ha\n"
            f"Vegetation cover: {_pct(total_vegetation_ha, analysis_area_ha)}%"
            f"{buf_desc}"
        ),
        ring=ring,
        box=box,
        overlay_name="Vegetation Type",
        png_bytes=png,
        legend_lines=["VEGETATION TYPE", *legend],
        input_line_coords=input_lines,
        input_polygon_coords=input_polys,
    )

    result = {
        "start_date": start_date,
        "end_date": end_date,
        "sentinel2_image_count": stack["s2_count"],
        "analysis_area_ha": analysis_area_ha,
        "total_vegetation_area_ha": total_vegetation_ha,
        "vegetation_cover_percent": _pct(total_vegetation_ha, analysis_area_ha),
        "stressed_vegetation_area_ha": stressed_ha,
        "stressed_vegetation_percent": _pct(stressed_ha, total_vegetation_ha),
        "percent_basis": "analysis_area",
        "categories": categories,
        "legend": VEG_TYPE_CLASSES,
        "export_scale_m": export_scale_m,
        "kml_bytes": kml_bytes,
    }
    if buffer_info:
        result["buffer"] = buffer_info
    return result


def analyze_vegetation_health(
    geometry: ee.Geometry,
    start_date: str,
    end_date: str,
    *,
    buffer_info: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    stack = _build_vegetation_stack(geometry, start_date, end_date)
    health_class = stack["health_class"]
    vegetation_health = stack["vegetation_health"]
    vegetation = stack["vegetation"]
    analysis_area = stack["geometry"]

    analysis_area_ha = round(
        float(analysis_area.area(1).divide(10000).getInfo()), 2
    )
    total_vegetation_ha = _area_ha(vegetation, analysis_area)

    mean_result = vegetation_health.reduceRegion(
        reducer=ee.Reducer.mean(),
        geometry=analysis_area,
        scale=SCALE,
        maxPixels=1e13,
        bestEffort=True,
        tileScale=4,
    )
    mean_health = round(
        float(
            ee.Number(
                ee.Algorithms.If(
                    mean_result.contains("Vegetation_Health_Score"),
                    mean_result.get("Vegetation_Health_Score"),
                    0,
                )
            ).getInfo()
        ),
        1,
    )

    categories = []
    for cls in VEG_HEALTH_CLASSES:
        cid = int(cls["id"])
        ha = _area_ha(health_class.eq(cid), analysis_area)
        categories.append(
            {
                "id": cid,
                "name": cls["name"],
                "score_range": cls["score_range"],
                "color": cls["color"],
                "area_ha": ha,
                "percent": _pct(ha, total_vegetation_ha),
            }
        )

    vis = _health_vis_image(health_class, stack["plant_type"], analysis_area)
    export_scale_m = _export_scale_for_geometry(analysis_area)
    png = _export_overlay_png(vis, analysis_area)
    ring = _ring_from_geometry(analysis_area)
    box = _aoi_box(analysis_area)
    legend = [
        f"{c['color']}  {c['score_range']}  {c['name']}" for c in VEG_HEALTH_CLASSES
    ]
    buf_desc = ""
    input_lines = None
    input_polys = None
    if buffer_info:
        buf_desc = (
            f"\nBuffer: {buffer_info['buffer_km']} km on each side\n"
            f"Analysis rectangle: {buffer_info['rectangle_width_km']} x "
            f"{buffer_info['rectangle_height_km']} km"
        )
        input_lines = buffer_info.get("input_line_coords")
        input_polys = buffer_info.get("input_polygon_coords")
    kml_bytes = _build_kml(
        title="Vegetation Health Score",
        description=(
            f"Vegetation health (50% NDVI · 30% NDMI · 20% EVI)\n"
            f"{start_date} to {end_date}\n"
            f"Vegetation area: {total_vegetation_ha} ha\n"
            f"Mean health score: {mean_health}"
            f"{buf_desc}"
        ),
        ring=ring,
        box=box,
        overlay_name="Vegetation Health",
        png_bytes=png,
        legend_lines=["VEGETATION HEALTH SCORE", *legend],
        input_line_coords=input_lines,
        input_polygon_coords=input_polys,
    )

    result = {
        "start_date": start_date,
        "end_date": end_date,
        "sentinel2_image_count": stack["s2_count"],
        "analysis_area_ha": analysis_area_ha,
        "total_vegetation_area_ha": total_vegetation_ha,
        "mean_vegetation_health_score": mean_health,
        "percent_basis": "total_vegetation_area",
        "categories": categories,
        "legend": VEG_HEALTH_CLASSES,
        "export_scale_m": export_scale_m,
        "kml_bytes": kml_bytes,
    }
    if buffer_info:
        result["buffer"] = buffer_info
    return result
