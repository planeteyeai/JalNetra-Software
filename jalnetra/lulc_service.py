"""
jalnetra.lulc_service — AlphaEarth annual LULC via Google Earth Engine.

Python port of the Mula-Mutha AlphaEarth LULC workflow (Dynamic World labels + RF).
"""
from __future__ import annotations

import base64
import math
import random
import time
import xml.etree.ElementTree as ET
from typing import Any, Callable, Dict, List, Optional, Tuple, TypeVar
from xml.dom import minidom

import ee
from shapely.geometry import MultiPolygon, Polygon

from jalnetra.ee_http import read_url
from jalnetra.kml_buffer import (
    _geom_to_ee,
    _line_coords_for_kml,
    load_geometry_from_kml_bytes,
)

T = TypeVar("T")

# Transient GEE / network failures (common under parallel getInfo load)
_EE_TRANSIENT_MARKERS = (
    "upstream",
    "502",
    "503",
    "504",
    "429",
    "timeout",
    "timed out",
    "connection reset",
    "unavailable",
    "internal error",
    "capacity",
    "rate limit",
    "please try again",
)

KML_NS = "http://www.opengis.net/kml/2.2"
BUFFER_METERS = 1000
ANALYSIS_SCALE = 10
DISPLAY_SCALE_M = 5
DISPLAY_SCALE_FALLBACK = [8.0, 10.0]
SMOOTH_RADIUS_M = 40  # soft class edges without square stairs
MAX_EXPORT_PIXELS = 12_000_000
KML_OVERLAY_COLOR = "ffffffff"
SAMPLE_POINTS_PER_CLASS = 400
TRAINING_PERCENT = 70
RF_TREES = 150

VALID_YEARS = [2021, 2022, 2023, 2024, 2025, 2026]
S2_YEARS = {2026}  # AlphaEarth may be unavailable — use Sentinel-2 RF
CLASS_IDS = [0, 1, 2, 3, 4]

LULC_CLASSES: List[Dict[str, Any]] = [
    {"id": 0, "name": "Forest", "color": "#006400"},
    {"id": 1, "name": "Crop Land", "color": "#E6A23C"},
    {"id": 2, "name": "Barren", "color": "#A9A9A9"},
    {"id": 3, "name": "Water Bodies", "color": "#2196F3"},
    {"id": 4, "name": "Settlements", "color": "#C62828"},
]

LULC_PALETTE = ["006400", "E6A23C", "A9A9A9", "2196F3", "C62828"]

ALPHA_EARTH = "GOOGLE/SATELLITE_EMBEDDING/V1/ANNUAL"
DYNAMIC_WORLD = "GOOGLE/DYNAMICWORLD/V1"


def _is_transient_ee_error(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return any(m in msg for m in _EE_TRANSIENT_MARKERS)


def _with_ee_retry(
    fn: Callable[[], T],
    *,
    retries: int = 4,
    label: str = "EE",
) -> T:
    """Retry transient Earth Engine upstream / capacity errors with backoff."""
    last: Optional[BaseException] = None
    for attempt in range(retries):
        try:
            return fn()
        except Exception as exc:
            last = exc
            if not _is_transient_ee_error(exc) or attempt >= retries - 1:
                raise
            delay = (2**attempt) + random.uniform(0.2, 1.0)
            print(
                f"[lulc] {label}: transient EE error "
                f"(attempt {attempt + 1}/{retries}): {exc}; "
                f"retrying in {delay:.1f}s"
            )
            time.sleep(delay)
    assert last is not None
    raise last


def _polygon_coords_for_kml(geom) -> List[List[List[float]]]:
    out: List[List[List[float]]] = []
    if isinstance(geom, Polygon):
        out.append([[float(x), float(y)] for x, y in geom.exterior.coords])
    elif isinstance(geom, MultiPolygon):
        for poly in geom.geoms:
            out.append([[float(x), float(y)] for x, y in poly.exterior.coords])
    return out


def lulc_analysis_geometry(
    kml_bytes: bytes,
) -> Tuple[ee.Geometry, ee.Geometry, Dict[str, Any]]:
    """Original AOI + 1 km geodesic buffer (GEE analysisZone)."""
    geom = load_geometry_from_kml_bytes(kml_bytes)
    clon, clat = float(geom.centroid.x), float(geom.centroid.y)
    input_ee = _geom_to_ee(geom)
    analysis_ee = input_ee.buffer(BUFFER_METERS)
    meta = {
        "buffer_m": BUFFER_METERS,
        "buffer_km": round(BUFFER_METERS / 1000.0, 1),
        "shape": "geodesic_buffer",
        "centroid": {"latitude": round(clat, 6), "longitude": round(clon, 6)},
        "input_line_coords": _line_coords_for_kml(geom),
        "input_polygon_coords": _polygon_coords_for_kml(geom),
    }
    return analysis_ee, input_ee, meta


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
    for candidate in (DISPLAY_SCALE_M, *DISPLAY_SCALE_FALLBACK, float(ANALYSIS_SCALE)):
        if _estimate_pixel_count(box, candidate) <= MAX_EXPORT_PIXELS:
            return candidate
    ratio = math.sqrt(_estimate_pixel_count(box, ANALYSIS_SCALE) / MAX_EXPORT_PIXELS)
    return round(ANALYSIS_SCALE * ratio, 1)


def _smooth_classified(image: ee.Image) -> ee.Image:
    """Light two-pass mode filter — soft class edges, no heavy blur."""
    first = image.focal_mode(radius=SMOOTH_RADIUS_M, units="meters")
    return first.focal_mode(radius=max(SMOOTH_RADIUS_M // 2, 10), units="meters")


def _class_area_ha(image: ee.Image, class_value: int, geometry: ee.Geometry) -> float:
    area_img = (
        ee.Image.pixelArea()
        .divide(10000)
        .updateMask(image.eq(class_value))
        .rename("area_ha")
    )
    result = area_img.reduceRegion(
        reducer=ee.Reducer.sum(),
        geometry=geometry,
        scale=ANALYSIS_SCALE,
        maxPixels=1e13,
        bestEffort=True,
        tileScale=8,
    )
    value = ee.Number(
        ee.Algorithms.If(result.contains("area_ha"), result.get("area_ha"), 0)
    ).getInfo()
    return round(float(value), 2)


def _pct(part: float, whole: float) -> float:
    if whole <= 0:
        return 0.0
    return round(part / whole * 100.0, 1)


def _make_dw_label(year: int, analysis_zone: ee.Geometry) -> ee.Image:
    start, end = _year_date_window(year)

    dw = (
        ee.ImageCollection(DYNAMIC_WORLD)
        .filterDate(start, end)
        .filterBounds(analysis_zone)
        .select("label")
    )
    if int(dw.size().getInfo() or 0) == 0:
        raise ValueError(f"No Dynamic World images found for {year} over the KML area.")

    annual_mode = dw.reduce(ee.Reducer.mode()).rename("dw_label")

    forest = annual_mode.eq(1)
    crop = annual_mode.eq(4)
    barren = annual_mode.eq(7)
    water = annual_mode.eq(0)
    settlement = annual_mode.eq(6)

    valid = forest.Or(crop).Or(barren).Or(water).Or(settlement)

    five_class = (
        ee.Image(0)
        .where(crop, 1)
        .where(barren, 2)
        .where(water, 3)
        .where(settlement, 4)
        .updateMask(valid)
        .rename("lulc")
        .clip(analysis_zone)
        .toByte()
    )
    return five_class


def _get_alpha(year: int, analysis_zone: ee.Geometry) -> ee.Image:
    start = ee.Date.fromYMD(year, 1, 1)
    end = start.advance(1, "year")

    collection = (
        ee.ImageCollection(ALPHA_EARTH)
        .filterDate(start, end)
        .filterBounds(analysis_zone)
    )
    count = int(collection.size().getInfo() or 0)
    if count == 0:
        raise ValueError(
            f"No AlphaEarth annual embeddings found for {year}. "
            f"Supported years: {VALID_YEARS[:-1]} (2026 uses Sentinel-2)."
        )

    return collection.mosaic().clip(analysis_zone)


def _mask_s2_lulc(image: ee.Image) -> ee.Image:
    scl = image.select("SCL")
    cloud_mask = (
        scl.neq(3)
        .And(scl.neq(8))
        .And(scl.neq(9))
        .And(scl.neq(10))
        .And(scl.neq(11))
    )
    return image.updateMask(cloud_mask).divide(10000).copyProperties(
        image, ["system:time_start"]
    )


def _year_date_window(year: int) -> Tuple[ee.Date, ee.Date]:
    """Full calendar year, or Jan 1 → tomorrow for the current year."""
    from datetime import date as _date

    start = ee.Date.fromYMD(year, 1, 1)
    today = _date.today()
    if year == today.year:
        end = ee.Date.fromYMD(today.year, today.month, today.day).advance(1, "day")
    else:
        end = start.advance(1, "year")
    return start, end


def _get_s2_features(year: int, analysis_zone: ee.Geometry) -> ee.Image:
    """Sentinel-2 median composite + spectral indices (for years without AlphaEarth)."""
    start, end = _year_date_window(year)
    collection = (
        ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
        .filterDate(start, end)
        .filterBounds(analysis_zone)
        .filter(ee.Filter.lt("CLOUDY_PIXEL_PERCENTAGE", 40))
        .map(_mask_s2_lulc)
    )
    count = int(collection.size().getInfo() or 0)
    if count == 0:
        raise ValueError(
            f"No Sentinel-2 scenes found for {year} over the KML area."
        )

    median = collection.median().clip(analysis_zone)
    bands = median.select(["B2", "B3", "B4", "B5", "B6", "B7", "B8", "B8A", "B11", "B12"])
    ndvi = bands.normalizedDifference(["B8", "B4"]).rename("NDVI")
    ndwi = bands.normalizedDifference(["B3", "B8"]).rename("NDWI")
    mndwi = bands.normalizedDifference(["B3", "B11"]).rename("MNDWI")
    ndbi = bands.normalizedDifference(["B11", "B8"]).rename("NDBI")
    bsi = bands.expression(
        "((SWIR + RED) - (NIR + BLUE)) / ((SWIR + RED) + (NIR + BLUE))",
        {
            "SWIR": bands.select("B11"),
            "RED": bands.select("B4"),
            "NIR": bands.select("B8"),
            "BLUE": bands.select("B2"),
        },
    ).rename("BSI")
    return bands.addBands([ndvi, ndwi, mndwi, ndbi, bsi])


def _rf_classify(
    features: ee.Image,
    label: ee.Image,
    year: int,
    analysis_zone: ee.Geometry,
    *,
    source: str,
) -> Dict[str, Any]:
    """Shared RF train/classify path (AlphaEarth or Sentinel-2 features)."""
    training_image = features.addBands(label)

    samples = training_image.stratifiedSample(
        numPoints=SAMPLE_POINTS_PER_CLASS,
        classBand="lulc",
        region=analysis_zone,
        scale=ANALYSIS_SCALE,
        classValues=CLASS_IDS,
        classPoints=[SAMPLE_POINTS_PER_CLASS] * 5,
        geometries=True,
        seed=year,
        tileScale=8,
    )
    sample_count = int(samples.size().getInfo() or 0)
    if sample_count == 0:
        raise ValueError(
            f"Could not collect training samples for LULC {year}. "
            "Try a different year or KML location."
        )

    samples_random = samples.randomColumn("random", year)
    training_samples = samples_random.filter(
        ee.Filter.lt("random", TRAINING_PERCENT / 100)
    )
    validation_samples = samples_random.filter(
        ee.Filter.gte("random", TRAINING_PERCENT / 100)
    )

    feature_bands = features.bandNames()
    classifier = (
        ee.Classifier.smileRandomForest(
            numberOfTrees=RF_TREES,
            variablesPerSplit=8,
            minLeafPopulation=2,
            bagFraction=0.7,
            seed=year,
        )
        .train(
            features=training_samples,
            classProperty="lulc",
            inputProperties=feature_bands,
        )
    )

    classified = (
        features.select(feature_bands)
        .classify(classifier)
        .rename(f"LULC_{year}")
        .clip(analysis_zone)
        .toByte()
    )

    validation = validation_samples.classify(classifier)
    confusion = validation.errorMatrix("lulc", "classification")
    accuracy = float(confusion.accuracy().getInfo() or 0)
    kappa = float(confusion.kappa().getInfo() or 0)

    return {
        "classified": classified,
        "overall_accuracy": round(accuracy, 4),
        "kappa": round(kappa, 4),
        "total_samples": sample_count,
        "training_samples": int(training_samples.size().getInfo() or 0),
        "validation_samples": int(validation_samples.size().getInfo() or 0),
        "source": source,
    }


def _classify_year_sentinel2(year: int, analysis_zone: ee.Geometry) -> Dict[str, Any]:
    """Sentinel-2 RF path (used for 2026 and as AlphaEarth fallback)."""
    features = _get_s2_features(year, analysis_zone)
    try:
        label = _make_dw_label(year, analysis_zone)
    except ValueError:
        # DW lag early in year — spectral pseudo-labels from S2 indices
        ndvi = features.select("NDVI")
        ndwi = features.select("NDWI")
        mndwi = features.select("MNDWI")
        ndbi = features.select("NDBI")
        water = ndwi.gt(0.2).Or(mndwi.gt(0.1))
        forest = ndvi.gt(0.55).And(water.Not())
        crop = ndvi.gt(0.35).And(ndvi.lte(0.55)).And(water.Not())
        settlement = ndbi.gt(0.0).And(ndvi.lt(0.3)).And(water.Not())
        barren = water.Not().And(forest.Not()).And(crop.Not()).And(
            settlement.Not()
        )
        valid = forest.Or(crop).Or(barren).Or(water).Or(settlement)
        label = (
            ee.Image(0)
            .where(crop, 1)
            .where(barren, 2)
            .where(water, 3)
            .where(settlement, 4)
            .updateMask(valid)
            .rename("lulc")
            .clip(analysis_zone)
            .toByte()
        )
    return _rf_classify(
        features, label, year, analysis_zone, source="sentinel2"
    )


def _classify_year(year: int, analysis_zone: ee.Geometry) -> Dict[str, Any]:
    if year in S2_YEARS:
        return _classify_year_sentinel2(year, analysis_zone)

    def _alpha_path() -> Dict[str, Any]:
        label = _make_dw_label(year, analysis_zone)
        alpha = _get_alpha(year, analysis_zone)
        return _rf_classify(
            alpha, label, year, analysis_zone, source="alphaearth"
        )

    try:
        return _with_ee_retry(
            _alpha_path, retries=3, label=f"LULC AlphaEarth {year}"
        )
    except Exception as exc:
        # Missing AlphaEarth layer or persistent upstream → Sentinel-2 fallback
        print(
            f"[lulc] AlphaEarth {year} failed ({exc}); "
            f"falling back to Sentinel-2 RF"
        )
        return _with_ee_retry(
            lambda: _classify_year_sentinel2(year, analysis_zone),
            retries=3,
            label=f"LULC Sentinel-2 {year}",
        )


def _lulc_vis_image(classified: ee.Image, analysis_area: ee.Geometry) -> ee.Image:
    """Fine-grid export with light smoothing — sharp classes, no square stairs."""
    export_scale = _export_scale_for_geometry(analysis_area)
    clipped = classified.clip(analysis_area)
    smoothed = _smooth_classified(clipped)
    scaled = smoothed.reproject(crs="EPSG:4326", scale=export_scale)
    return scaled.visualize(min=0, max=4, palette=LULC_PALETTE).updateMask(
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
    input_line_coords: Optional[List[List[List[float]]]] = None,
    input_polygon_coords: Optional[List[List[List[float]]]] = None,
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


def _build_year_result(
    geometry: ee.Geometry,
    year: int,
    *,
    analysis_area_ha: float,
    box: Dict[str, float],
    buffer_info: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Run existing single-year LULC classification + smoothed KML (logic unchanged)."""
    result = _classify_year(year, geometry)
    classified = result["classified"]

    categories = []
    for cls in LULC_CLASSES:
        cid = int(cls["id"])
        ha = _with_ee_retry(
            lambda c=cid: _class_area_ha(classified, c, geometry),
            retries=3,
            label=f"LULC area class {cid} {year}",
        )
        categories.append(
            {
                "id": cid,
                "name": cls["name"],
                "color": cls["color"],
                "area_ha": ha,
                "percent": _pct(ha, analysis_area_ha),
            }
        )

    vis = _lulc_vis_image(classified, geometry)
    export_scale_m = _export_scale_for_geometry(geometry)
    png = _with_ee_retry(
        lambda: _export_overlay_png(vis, geometry),
        retries=3,
        label=f"LULC PNG {year}",
    )
    legend = [f"{c['color']}  {c['name']}" for c in LULC_CLASSES]

    buf_desc = ""
    input_lines = None
    input_polys = None
    if buffer_info:
        buf_desc = f"\nBuffer: {buffer_info['buffer_km']} km (geodesic)"
        input_lines = buffer_info.get("input_line_coords")
        input_polys = buffer_info.get("input_polygon_coords")

    source = result.get("source", "alphaearth")
    source_label = (
        "Sentinel-2 RF" if source == "sentinel2" else "AlphaEarth annual LULC"
    )

    kml_bytes = _build_kml(
        title=f"LULC {year}",
        description=(
            f"{source_label} · {year}\n"
            f"Analysis area: {analysis_area_ha} ha\n"
            f"Overall accuracy: {result['overall_accuracy']}\n"
            f"Kappa: {result['kappa']}"
            f"{buf_desc}"
        ),
        box=box,
        overlay_name=f"LULC {year}",
        png_bytes=png,
        legend_lines=["LULC CLASSES", *legend],
        input_line_coords=input_lines,
        input_polygon_coords=input_polys,
    )

    return {
        "year": year,
        "source": source,
        "overall_accuracy": result["overall_accuracy"],
        "kappa": result["kappa"],
        "total_samples": result["total_samples"],
        "training_samples": result["training_samples"],
        "validation_samples": result["validation_samples"],
        "percent_basis": "analysis_area",
        "categories": categories,
        "legend": LULC_CLASSES,
        "export_scale_m": export_scale_m,
        "kml_filename": f"lulc_{year}.kml",
        "kml_bytes": kml_bytes,
    }


def analyze_lulc(
    geometry: ee.Geometry,
    *,
    buffer_info: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Classify annual LULC for default years 2021–2026.

    2021–2025: AlphaEarth embeddings + Dynamic World RF (unchanged).
    2026: Sentinel-2 spectral features + RF (AlphaEarth often unavailable).
    Returns six yearly layers with smoothed KML overlays.
    """
    analysis_area = geometry
    analysis_area_ha = round(
        float(analysis_area.area(1).divide(10000).getInfo()), 2
    )
    box = _aoi_box(analysis_area)

    yearly: Dict[str, Dict[str, Any]] = {}
    errors: Dict[str, str] = {}

    # Sequential years — parallel getInfo() commonly triggers EE "Upstream error"
    for year in VALID_YEARS:
        key = str(year)
        try:
            yearly[key] = _build_year_result(
                analysis_area,
                year,
                analysis_area_ha=analysis_area_ha,
                box=box,
                buffer_info=buffer_info,
            )
        except Exception as exc:
            errors[key] = str(exc)

    years_out: Dict[str, Any] = {}
    for year in VALID_YEARS:
        key = str(year)
        if key in yearly:
            layer = yearly[key]
            years_out[key] = {k: v for k, v in layer.items() if k != "kml_bytes"}
        else:
            years_out[key] = {
                "year": year,
                "error": errors.get(key, "LULC analysis failed for this year."),
            }

    if not yearly:
        raise ValueError(
            "LULC analysis failed for all years 2021–2026. "
            + "; ".join(f"{y}: {errors[y]}" for y in sorted(errors))
        )

    result: Dict[str, Any] = {
        "period": "2021–2026",
        "years_requested": VALID_YEARS,
        "analysis_area_ha": analysis_area_ha,
        "percent_basis": "analysis_area",
        "legend": LULC_CLASSES,
        "years": years_out,
        "notes": {
            "default_years": "Years are fixed to 2021–2026 (no year input).",
            "year_2026": "Sentinel-2 median + RF (AlphaEarth not required).",
            "alphaearth_fallback": (
                "If AlphaEarth/upstream fails for 2021–2025, that year "
                "falls back to Sentinel-2 RF automatically."
            ),
            "smoothing": (
                "Light focal_mode + 5 m export grid for smooth class edges "
                "without square stairs or heavy blur."
            ),
        },
    }
    for year in VALID_YEARS:
        key = str(year)
        if key in yearly:
            result[f"y{year}_kml_bytes"] = yearly[key]["kml_bytes"]
    if buffer_info:
        result["buffer"] = buffer_info
    if errors:
        result["year_errors"] = errors
    return result