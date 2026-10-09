"""
jalnetra.lithology_service — Lithological / geological surface-material interpretation.

Python port of the Mithi River lithology GEE workflow (Sentinel-2 + DEM + K-Means).
"""
from __future__ import annotations

import base64
import math
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional, Tuple
from xml.dom import minidom

import ee
from pyproj import Transformer
from shapely.ops import transform

from jalnetra.ee_http import read_url
from jalnetra.kml_buffer import (
    _geom_to_ee,
    _line_coords_for_kml,
    _polygon_coords_for_kml,
    _utm_epsg,
    load_geometry_from_kml_bytes,
)

KML_NS = "http://www.opengis.net/kml/2.2"
SCALE = 20
TILE_SCALE = 4
CLOUD_LIMIT = 40
NUM_CLUSTERS = 8
BUFFER_METERS = 50  # 50 m from KML border (not from centre)
CLUSTER_TRAINING_SAMPLE_SIZE = 1200
CLUSTER_SEED = 42
S2_SCENE_LIMIT = 30
LARGE_AREA_HA = 2500
DISPLAY_SCALE_M = 5
DISPLAY_SCALE_FALLBACK = [8.0, 10.0]
SMOOTH_RADIUS_M = 70
THUMB_MAX_DIMENSIONS = 2000
MAX_EXPORT_PIXELS = 10_000_000
KML_OVERLAY_COLOR = "ffffffff"

# Display palette: water + 8 K-Means cluster colours (matches GEE layer 09)
LITHOLOGY_PALETTE = [
    "5D4037",  # silt — dark brown (was water)
    "8B0000",
    "FFD700",
    "FF8C00",
    "9370DB",
    "00A6A6",
    "F4A460",
    "808080",
    "8B4513",
]

LITHOLOGY_CLASSES: List[Dict[str, Any]] = [
    {
        "id": 0,
        "name": "Silt",
        "color": "#5D4037",
    },
    {
        "id": 1,
        "name": "Basaltic / Fresh Basalt Spectral Zone",
        "color": "#8B0000",
    },
    {
        "id": 2,
        "name": "Weathered Basalt",
        "color": "#FFD700",
    },
    {
        "id": 3,
        "name": "Lateritic / Ferruginous Zone",
        "color": "#FF0000",
    },
    {
        "id": 4,
        "name": "Clay-Rich / Altered Zone",
        "color": "#9370DB",
    },
    {
        "id": 5,
        "name": "Alluvial / Sandy-Clayey Sediment",
        "color": "#F4A460",
    },
    {
        "id": 6,
        "name": "Estuarine / Tidal Sediment",
        "color": "#00A6A6",
    },
    {
        "id": 7,
        "name": "Mixed Weathered Geological Material",
        "color": "#808080",
    },
    {
        "id": 8,
        "name": "Exposed / Bright Mineral Surface",
        "color": "#FFFFFF",
    },
]


def _buffered_polygon_coords(geom) -> List[List[List[float]]]:
    """UTM metre buffer outline — matches GEE geodesic buffer for KML display."""
    clon, clat = float(geom.centroid.x), float(geom.centroid.y)
    epsg = _utm_epsg(clon, clat)
    fwd = Transformer.from_crs("EPSG:4326", f"EPSG:{epsg}", always_xy=True).transform
    inv = Transformer.from_crs(f"EPSG:{epsg}", "EPSG:4326", always_xy=True).transform
    buffered = transform(fwd, geom).buffer(BUFFER_METERS)
    return _polygon_coords_for_kml(transform(inv, buffered))


def lithology_geometry(kml_bytes: bytes) -> Tuple[ee.Geometry, ee.Geometry, Dict[str, Any]]:
    """Analysis AOI: input KML + 50 m border buffer (from geometry edge)."""
    geom = load_geometry_from_kml_bytes(kml_bytes)
    clon, clat = float(geom.centroid.x), float(geom.centroid.y)
    input_ee = _geom_to_ee(geom)
    analysis_ee = input_ee.buffer(BUFFER_METERS)
    meta = {
        "buffer_m": BUFFER_METERS,
        "buffer_km": round(BUFFER_METERS / 1000.0, 3),
        "shape": "border_buffer",
        "centroid": {"latitude": round(clat, 6), "longitude": round(clon, 6)},
        "input_line_coords": _line_coords_for_kml(geom),
        "input_polygon_coords": _polygon_coords_for_kml(geom),
        "analysis_polygon_coords": _buffered_polygon_coords(geom),
    }
    return analysis_ee, input_ee, meta


def _analysis_scale_for_area(area_ha: float) -> int:
    """Coarser scale on large AOIs — keeps K-Means tractable."""
    if area_ha > 8000:
        return 40
    if area_ha > LARGE_AREA_HA:
        return 30
    return SCALE


def _mask_s2(image: ee.Image) -> ee.Image:
    scl = image.select("SCL")
    mask = (
        scl.neq(3)
        .And(scl.neq(8))
        .And(scl.neq(9))
        .And(scl.neq(10))
        .And(scl.neq(11))
    )
    return (
        image.updateMask(mask)
        .select(["B2", "B3", "B4", "B5", "B6", "B7", "B8", "B8A", "B11", "B12"])
        .divide(10000)
        .copyProperties(image, ["system:time_start"])
    )


def _safe_ratio(numerator: ee.Image, denominator: ee.Image, name: str) -> ee.Image:
    epsilon = 0.0001
    den = denominator.max(0).max(epsilon)
    num = numerator.max(0)
    return num.divide(den).rename(name)


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


def _export_scale_for_box(box: Dict[str, float]) -> float:
    candidates = [DISPLAY_SCALE_M, *DISPLAY_SCALE_FALLBACK, float(SCALE)]
    for candidate in candidates:
        if _estimate_pixel_count(box, candidate) <= MAX_EXPORT_PIXELS:
            return candidate
    ratio = math.sqrt(_estimate_pixel_count(box, SCALE) / MAX_EXPORT_PIXELS)
    return round(SCALE * ratio, 1)


def _export_scale_for_geometry(geometry: ee.Geometry) -> float:
    return _export_scale_for_box(_aoi_box(geometry))


def _smooth_classified(image: ee.Image, *, light: bool = False) -> ee.Image:
    """Mode filter — single pass on large AOIs, two-pass otherwise."""
    radius = SMOOTH_RADIUS_M // 2 if light else SMOOTH_RADIUS_M
    first = image.focal_mode(radius=radius, units="meters")
    if light:
        return first
    return first.focal_mode(radius=max(radius // 2, 12), units="meters")


def _aoi_mask_image(aoi: ee.Geometry) -> ee.Image:
    return ee.Image.constant(1).clip(aoi).selfMask()


def _pct(part: float, whole: float) -> float:
    if whole <= 0:
        return 0.0
    return round(part / whole * 100.0, 1)


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
        tileScale=TILE_SCALE,
    ).getInfo() or {}
    areas: Dict[int, float] = {}
    for group in result.get("groups", []) or []:
        cid = int(group["class"])
        areas[cid] = round(float(group["sum"]), 2)
    return areas


def _lithology_vis_image(
    lithology_map: ee.Image,
    aoi: ee.Geometry,
    *,
    export_scale: float,
    light_smooth: bool,
) -> ee.Image:
    """Smoothed class overlay — transparent outside water / lithology pixels."""
    aoi_mask = _aoi_mask_image(aoi)
    clipped = lithology_map.clip(aoi)
    region_mask = clipped.mask()

    smoothed = _smooth_classified(clipped, light=light_smooth)
    masked = smoothed.updateMask(region_mask).updateMask(aoi_mask)

    scaled = masked.reproject(crs="EPSG:4326", scale=export_scale)
    return scaled.visualize(min=0, max=NUM_CLUSTERS, palette=LITHOLOGY_PALETTE).updateMask(
        scaled.mask()
    )


def _export_overlay_png(
    vis_image: ee.Image, geometry: ee.Geometry, export_scale: float
) -> bytes:
    """Fast thumbnail export first; scale-based fallback."""
    for params in (
        {"region": geometry, "dimensions": THUMB_MAX_DIMENSIONS, "format": "png"},
        {"region": geometry, "scale": export_scale, "format": "png"},
    ):
        try:
            url = vis_image.getThumbURL(params)
            return read_url(url, timeout=600)
        except Exception:
            continue
    raise RuntimeError("Failed to export lithology overlay PNG from Earth Engine.")


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
    analysis_polygon_coords: Optional[List[List[List[float]]]] = None,
) -> bytes:
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

    if analysis_polygon_coords:
        for idx, coords in enumerate(analysis_polygon_coords):
            pm_buf = _kml_el(doc, "Placemark")
            _kml_el(
                pm_buf,
                "name",
                "Analysis buffer (50 m)" if idx == 0 else f"Analysis buffer {idx + 1}",
            )
            buf_poly = _kml_el(pm_buf, "Polygon")
            buf_outer = _kml_el(buf_poly, "outerBoundaryIs")
            buf_lr = _kml_el(buf_outer, "LinearRing")
            _kml_el(
                buf_lr,
                "coordinates",
                " ".join(f"{lon},{lat},0" for lon, lat in coords),
            )
            buf_style = _kml_el(pm_buf, "Style")
            buf_ls = _kml_el(buf_style, "LineStyle")
            _kml_el(buf_ls, "color", "ffff0000")
            _kml_el(buf_ls, "width", "2")
            buf_ps = _kml_el(buf_style, "PolyStyle")
            _kml_el(buf_ps, "color", "00000000")
            _kml_el(buf_ps, "fill", "0")
            _kml_el(buf_ps, "outline", "1")

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
            _kml_el(in_ls, "width", "4")
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
            _kml_el(in_ls, "width", "5")

    xml_bytes = ET.tostring(root, encoding="utf-8")
    return minidom.parseString(xml_bytes).toprettyxml(indent="  ", encoding="utf-8")


def analyze_lithology(
    aoi_geometry: ee.Geometry,
    start_date: str,
    end_date: str,
    *,
    buffer_info: Optional[Dict[str, Any]] = None,
    dry_season_only: bool = True,
) -> Dict[str, Any]:
    """
    Lithological spectral interpretation via Sentinel-2 median + K-Means clustering.

    `dry_season_only=False` skips the Nov–Apr month filter (used when the API
    passes an auto-selected recent clear-scene date).

    Water (class 0) and eight unsupervised lithology clusters (classes 1–8).
    Dense vegetation and invalid pixels remain transparent in the KML overlay.
    """
    analysis_area_ha = round(
        float(aoi_geometry.area(1).divide(10000).getInfo()), 2
    )
    analysis_scale = _analysis_scale_for_area(analysis_area_ha)
    light_smooth = analysis_area_ha > LARGE_AREA_HA
    box = _aoi_box(aoi_geometry)
    export_scale_m = _export_scale_for_box(box)

    s2 = (
        ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
        .filterBounds(aoi_geometry)
        .filterDate(start_date, end_date)
        .filter(ee.Filter.lte("CLOUDY_PIXEL_PERCENTAGE", CLOUD_LIMIT))
    )
    if dry_season_only:
        s2 = s2.filter(
            ee.Filter.Or(
                ee.Filter.calendarRange(11, 12, "month"),
                ee.Filter.calendarRange(1, 4, "month"),
            )
        )
    s2 = s2.sort("CLOUDY_PIXEL_PERCENTAGE").limit(S2_SCENE_LIMIT).map(_mask_s2)

    image_count = int(s2.size().getInfo() or 0)
    if image_count == 0:
        raise ValueError(
            f"No {'dry-season ' if dry_season_only else ''}Sentinel-2 scenes found "
            f"between {start_date} and {end_date} over the KML area"
            + (" (Nov–Dec or Jan–Apr months only)." if dry_season_only else ".")
        )

    image = s2.median().clip(aoi_geometry)

    b2 = image.select("B2").rename("B2_Blue")
    b3 = image.select("B3").rename("B3_Green")
    b4 = image.select("B4").rename("B4_Red")
    b5 = image.select("B5").rename("B5_RedEdge1")
    b6 = image.select("B6").rename("B6_RedEdge2")
    b7 = image.select("B7").rename("B7_RedEdge3")
    b8 = image.select("B8").rename("B8_NIR")
    b8a = image.select("B8A").rename("B8A_RedEdge4")
    b11 = image.select("B11").rename("B11_SWIR1")
    b12 = image.select("B12").rename("B12_SWIR2")

    ndvi = image.normalizedDifference(["B8", "B4"]).rename("NDVI")
    ndwi = image.normalizedDifference(["B3", "B8"]).rename("NDWI")
    mndwi = image.normalizedDifference(["B3", "B11"]).rename("MNDWI")

    bsi = image.expression(
        "((SWIR + RED) - (NIR + BLUE)) / ((SWIR + RED) + (NIR + BLUE))",
        {
            "SWIR": image.select("B11"),
            "RED": image.select("B4"),
            "NIR": image.select("B8"),
            "BLUE": image.select("B2"),
        },
    ).rename("BSI")

    iron_oxide = _safe_ratio(image.select("B4"), image.select("B2"), "Iron_Oxide_Index")
    clay_index = _safe_ratio(image.select("B11"), image.select("B12"), "Clay_Mineral_Index")
    ferrous_index = _safe_ratio(image.select("B11"), image.select("B8"), "Ferrous_Mineral_Index")
    swir2_nir = _safe_ratio(image.select("B12"), image.select("B8"), "SWIR2_NIR")
    swir1_nir = _safe_ratio(image.select("B11"), image.select("B8"), "SWIR1_NIR")
    swir2_red = _safe_ratio(image.select("B12"), image.select("B4"), "SWIR2_Red")
    swir1_red = _safe_ratio(image.select("B11"), image.select("B4"), "SWIR1_Red")
    nir_red = _safe_ratio(image.select("B8"), image.select("B4"), "NIR_Red")
    blue_red = _safe_ratio(image.select("B2"), image.select("B4"), "Blue_Red_Ratio")
    red_green = _safe_ratio(image.select("B4"), image.select("B3"), "Red_Green_Ratio")
    re1_re2 = _safe_ratio(image.select("B5"), image.select("B6"), "RedEdge1_RedEdge2")
    re2_re3 = _safe_ratio(image.select("B6"), image.select("B7"), "RedEdge2_RedEdge3")
    re3_nir = _safe_ratio(image.select("B7"), image.select("B8"), "RedEdge3_NIR")

    brightness = (
        image.select(["B2", "B3", "B4", "B8", "B11", "B12"])
        .reduce(ee.Reducer.mean())
        .rename("Spectral_Brightness")
    )
    swir_contrast = image.expression(
        "(SWIR2 - NIR) / (SWIR2 + NIR + 0.0001)",
        {"SWIR2": image.select("B12"), "NIR": image.select("B8")},
    ).rename("SWIR_NIR_Contrast")

    water_mask = ndwi.gt(0.15).Or(mndwi.gt(0.0))
    vegetation_mask = ndvi.gt(0.45)

    spectral_base = image.select(
        ["B2", "B3", "B4", "B5", "B6", "B7", "B8", "B8A", "B11", "B12"]
    )
    valid_spectral_mask = spectral_base.mask().reduce(ee.Reducer.min())

    lithology_mask = (
        valid_spectral_mask.And(water_mask.Not()).And(vegetation_mask.Not())
    )

    lithology_predictors = ee.Image.cat(
        [
            b2,
            b3,
            b4,
            b5,
            b6,
            b7,
            b8,
            b8a,
            b11,
            b12,
            bsi,
            iron_oxide,
            clay_index,
            ferrous_index,
            swir2_nir,
            swir1_nir,
            swir2_red,
            swir1_red,
            nir_red,
            blue_red,
            red_green,
            re1_re2,
            re2_re3,
            re3_nir,
            brightness,
            swir_contrast,
        ]
    ).clip(aoi_geometry)

    if analysis_scale > SCALE:
        lithology_predictors = lithology_predictors.reproject(
            crs="EPSG:4326", scale=analysis_scale
        )
        lithology_mask = lithology_mask.reproject(crs="EPSG:4326", scale=analysis_scale)
        water_mask = water_mask.reproject(crs="EPSG:4326", scale=analysis_scale)

    training_sample = lithology_predictors.updateMask(lithology_mask).sample(
        region=aoi_geometry,
        scale=analysis_scale,
        numPixels=CLUSTER_TRAINING_SAMPLE_SIZE,
        seed=CLUSTER_SEED,
        tileScale=TILE_SCALE,
        geometries=False,
    )

    clusterer = ee.Clusterer.wekaKMeans(NUM_CLUSTERS).train(training_sample)

    raw_clusters = (
        lithology_predictors.updateMask(lithology_mask)
        .cluster(clusterer)
        .rename("Raw_Lithology_Cluster")
    )
    cluster_id = raw_clusters.add(1).rename("Lithology_Cluster_ID")

    lithology_map = (
        cluster_id.where(water_mask, 0).clip(aoi_geometry).rename("Lithology_Class")
    )

    class_areas = _class_areas_ha(lithology_map, aoi_geometry, analysis_scale)
    classified_area_ha = round(sum(class_areas.values()), 2)

    categories = []
    for cls in LITHOLOGY_CLASSES:
        cid = int(cls["id"])
        ha = class_areas.get(cid, 0.0)
        categories.append(
            {
                "id": cid,
                "name": cls["name"],
                "color": cls["color"],
                "area_ha": ha,
                "percent": _pct(ha, classified_area_ha),
            }
        )

    vis = _lithology_vis_image(
        lithology_map,
        aoi_geometry,
        export_scale=export_scale_m,
        light_smooth=light_smooth,
    )
    png = _export_overlay_png(vis, aoi_geometry, export_scale_m)
    legend = [f"{c['color']}  {c['id']} = {c['name']}" for c in LITHOLOGY_CLASSES]

    buf_desc = ""
    if buffer_info:
        buf_desc = (
            f"\nBuffer: {buffer_info.get('buffer_m', BUFFER_METERS)} m from KML border"
        )

    input_lines = None
    input_polys = None
    analysis_polys = None
    if buffer_info:
        input_lines = buffer_info.get("input_line_coords")
        input_polys = buffer_info.get("input_polygon_coords")
        analysis_polys = buffer_info.get("analysis_polygon_coords")

    kml_bytes = _build_kml(
        title="Lithological Spectral Interpretation",
        description=(
            f"Lithology · {start_date} to {end_date}\n"
            f"Sentinel-2 {'dry-season median' if dry_season_only else 'clear scene'}"
            f" + K-Means ({NUM_CLUSTERS} clusters)\n"
            f"Analysis area: {analysis_area_ha} ha · Resolution: {analysis_scale} m"
            f"{buf_desc}\n"
            "Cyan = input KML · Red outline = 50 m analysis buffer\n"
            "Spectral interpretation — field validation required.\n"
            "K-Means cluster IDs are arbitrary; use cluster statistics to interpret geology."
        ),
        box=box,
        overlay_name="Lithology Map",
        png_bytes=png,
        legend_lines=["LITHOLOGICAL INTERPRETATION", *legend],
        input_line_coords=input_lines,
        input_polygon_coords=input_polys,
        analysis_polygon_coords=analysis_polys,
    )

    result: Dict[str, Any] = {
        "start_date": start_date,
        "end_date": end_date,
        "analysis_area_ha": analysis_area_ha,
        "classified_area_ha": classified_area_ha,
        "percent_basis": "classified_area",
        "resolution_m": analysis_scale,
        "num_clusters": NUM_CLUSTERS,
        "cloud_limit_percent": CLOUD_LIMIT,
        "dry_season_months": (
            "November–December and January–April" if dry_season_only else None
        ),
        "sentinel2_image_count": image_count,
        "kmeans_training_samples": CLUSTER_TRAINING_SAMPLE_SIZE,
        "categories": categories,
        "legend": LITHOLOGY_CLASSES,
        "export_scale_m": export_scale_m,
        "kml_bytes": kml_bytes,
        "notes": {
            "interpretation": (
                "Results are spectral interpretations and should be validated against "
                "geological maps, field observations, or laboratory samples."
            ),
            "kmeans": (
                "K-Means cluster IDs are arbitrary; cluster numbers do not automatically "
                "equal specific lithology types without spectral validation."
            ),
            "excluded_pixels": (
                "Dense vegetation (NDVI > 0.45) and invalid spectral pixels are excluded "
                "from clustering and appear transparent in the KML overlay."
            ),
        },
    }
    if buffer_info:
        result["buffer"] = buffer_info
    return result
