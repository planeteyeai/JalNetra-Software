"""
jalnetra.fishing_point_service — Fishing suitability hotspots → lat/lon + KML.

Python port of Mithi River Fishing Suitability v1.1 (remote-sensing proxy).
Input: KML AOI. Output: robust fishing hotspot points + one KML (fish icons).

Does NOT measure fish abundance, catch, DO, BOD/COD, or real fishing activity.
"""
from __future__ import annotations

import random
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional, Tuple
from xml.dom import minidom

import ee

from jalnetra.kml_buffer import _geom_to_ee, load_geometry_from_kml_bytes

KML_NS = "http://www.opengis.net/kml/2.2"

# Public fish icon (Google Earth KML shapes); works in Google Earth / Earth Pro.
FISH_ICON_HREF = "http://maps.google.com/mapfiles/kml/shapes/fishing.png"

BUFFER_METERS = 60
ANALYSIS_SCALE = 20
EXPORT_SCALE = 10
CLOUD_PROB_MAX = 40
GRID_CELL_METERS = 250
REDUCE_TILE_SCALE = 4
EXPECTED_CLEAR_OBS = 15
MONTE_CARLO_ITERATIONS = 100
ENABLE_MONTE_CARLO = True

# End dates are exclusive: dry = Mar–Apr 2026, wet = Jul–Sep 2026.
DRY_WINDOWS: List[List[str]] = [
    ["2026-03-01", "2026-05-01"],
]

WET_WINDOWS: List[List[str]] = [
    ["2026-07-01", "2026-10-01"],
]

# 2026 dry season: use only 1 Sentinel-2 scene across the dry window
DRY_2026_SINGLE_IMAGE = True
DRY_2026_START, DRY_2026_END = DRY_WINDOWS[-1]

MNDWI_WATER_THRESHOLD = 0.05
NDVI_WATER_MAX = 0.35
SAR_VH_THRESHOLD = -18
SAR_SPECKLE_RADIUS = 2
SLOPE_IDEAL = 5
SLOPE_MAX = 35
HIGH_MOISTURE_NDMI = 0.30
THERMAL_ANOMALY_C = 1.5
TURBIDITY_ANOMALY_THRESHOLD = 0.15
ACCESS_DECAY_M = 500
ESTUARY_DECAY_M = 1000
BRIDGE_DECAY_M = 500
PERCENTILE_LOW = 5
PERCENTILE_HIGH = 95
RAINFALL_ANTECEDENT_DAYS = 5

FISHING_WEIGHTS = {
    "waterPersistence": 0.20,
    "waterStability": 0.10,
    "habitat": 0.20,
    "accessibility": 0.20,
    "morphology": 0.10,
    "seasonalWater": 0.10,
    "locationAccess": 0.10,
}

POLLUTION_WEIGHTS = {
    "turbidity": 0.25,
    "algalStress": 0.25,
    "thermalStress": 0.15,
    "anthropogenic": 0.20,
    "dischargeSignal": 0.15,
}

CONFIDENCE_WEIGHTS = {
    "observation": 0.40,
    "sensorAgreement": 0.35,
    "demValidity": 0.25,
}


def _local_name(tag: str) -> str:
    return tag.split("}")[-1] if "}" in tag else tag


def _safe_get(stats: Dict[str, Any], key: str, fallback: float = 0.0) -> float:
    if not stats or key not in stats or stats[key] is None:
        return float(fallback)
    try:
        return float(stats[key])
    except (TypeError, ValueError):
        return float(fallback)


def _parse_kml_points(kml_bytes: bytes) -> List[Tuple[str, float, float]]:
    """Named Point placemarks from KML (access / bridge candidates)."""
    try:
        root = ET.fromstring(kml_bytes)
    except ET.ParseError:
        return []
    points: List[Tuple[str, float, float]] = []
    for pm in root.iter():
        if _local_name(pm.tag) != "Placemark":
            continue
        name = None
        for child in list(pm):
            if _local_name(child.tag) == "name" and child.text:
                name = child.text.strip()
                break
        lon = lat = None
        has_point = False
        for child in pm.iter():
            tag = _local_name(child.tag)
            if tag == "Point":
                has_point = True
            if tag == "coordinates" and has_point and child.text:
                token = child.text.strip().split()[0]
                parts = token.split(",")
                if len(parts) >= 2:
                    lon, lat = float(parts[0]), float(parts[1])
        if has_point and lon is not None and lat is not None:
            points.append((name or f"Access {len(points) + 1}", lon, lat))
    return points


def _sample_access_along_aoi(
    aoi: ee.Geometry, n: int = 5
) -> List[Tuple[str, float, float]]:
    """Fallback access landmarks along AOI when KML has no Point placemarks."""
    coords = aoi.bounds().coordinates().getInfo()[0]
    # Use bounds ring + centroid as rough landmarks
    lons = [c[0] for c in coords]
    lats = [c[1] for c in coords]
    west, east = min(lons), max(lons)
    south, north = min(lats), max(lats)
    clon = (west + east) / 2
    clat = (south + north) / 2
    samples = [
        ("AOI NW", west, north),
        ("AOI NE", east, north),
        ("AOI centre", clon, clat),
        ("AOI SW", west, south),
        ("AOI SE / outlet proxy", east, south),
    ]
    return samples[:n]


def fishing_point_geometry(
    kml_bytes: bytes,
) -> Tuple[ee.Geometry, ee.Geometry, Dict[str, Any], List[Tuple[str, float, float]]]:
    """AOI from KML + 60 m border buffer (CONFIG.bufferMeters)."""
    geom = load_geometry_from_kml_bytes(kml_bytes)
    clon, clat = float(geom.centroid.x), float(geom.centroid.y)
    aoi = _geom_to_ee(geom)
    aoi_buffered = aoi.buffer(BUFFER_METERS)
    access = _parse_kml_points(kml_bytes)
    meta = {
        "buffer_m": BUFFER_METERS,
        "centroid": {"latitude": round(clat, 6), "longitude": round(clon, 6)},
    }
    return aoi_buffered, aoi, meta, access


def _build_date_filter(windows: List[List[str]]) -> ee.Filter:
    filters = [ee.Filter.date(w[0], w[1]) for w in windows]
    return ee.Filter.Or(*filters)


def _mask_clouds(img: ee.Image) -> ee.Image:
    cloud_mask = ee.Image(img.get("cloud_mask")).select("probability")
    return (
        img.updateMask(cloud_mask.lt(CLOUD_PROB_MAX))
        .divide(10000)
        .copyProperties(img, ["system:time_start", "system:index"])
    )


def _get_masked_collection(
    windows: List[List[str]], aoi_buffered: ee.Geometry
) -> ee.ImageCollection:
    date_filter = _build_date_filter(windows)
    s2 = ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
    s2_clouds = ee.ImageCollection("COPERNICUS/S2_CLOUD_PROBABILITY")
    primary = s2.filterBounds(aoi_buffered).filter(date_filter)
    secondary = s2_clouds.filterBounds(aoi_buffered).filter(date_filter)
    joined = ee.Join.saveFirst("cloud_mask").apply(
        primary=primary,
        secondary=secondary,
        condition=ee.Filter.equals(
            leftField="system:index", rightField="system:index"
        ),
    )
    return ee.ImageCollection(joined).map(_mask_clouds)


def _limit_2026_dry_to_one_image(
    collection: ee.ImageCollection, aoi_buffered: ee.Geometry
) -> Tuple[ee.ImageCollection, str]:
    """
    For dry 2026 (Mar–May): keep a single scene across the 3 months.
    If 2026 has no imagery, substitute one scene from 2025 Mar–May.
    Other dry-window scenes (2024, 2025 full windows) are unchanged.
    """
    pre_2026 = collection.filterDate("2024-03-01", DRY_2026_START)
    note = "2026 dry: 1 Sentinel-2 image (Mar–Apr)"

    if not DRY_2026_SINGLE_IMAGE:
        return collection, note

    dry_2026 = (
        collection.filterDate(DRY_2026_START, DRY_2026_END)
        .sort("CLOUDY_PIXEL_PERCENTAGE")
        .limit(1)
    )
    n2026 = int(dry_2026.size().getInfo() or 0)
    if n2026 == 0:
        dry_2026 = collection.filterDate(DRY_2026_START, DRY_2026_END).limit(1)
        n2026 = int(dry_2026.size().getInfo() or 0)

    if n2026 == 0:
        # No 2026 imagery — use one 2025 dry scene as the 2026 slot
        dry_2026 = (
            collection.filterDate("2025-03-01", "2025-05-15")
            .sort("CLOUDY_PIXEL_PERCENTAGE")
            .limit(1)
        )
        if int(dry_2026.size().getInfo() or 0) == 0:
            dry_2026 = collection.filterDate("2025-03-01", "2025-05-15").limit(1)
        note = (
            "2026 dry: no Sentinel-2 imagery — used 1 image from 2025 Mar–May instead"
        )
        if int(dry_2026.size().getInfo() or 0) == 0:
            note = "2026 dry: no 2026 or 2025 substitute image available"
            return ee.ImageCollection(pre_2026), note

    return ee.ImageCollection(pre_2026.merge(dry_2026)), note


def _mndwi(img: ee.Image) -> ee.Image:
    return img.normalizedDifference(["B3", "B11"]).rename("MNDWI")


def _ndvi(img: ee.Image) -> ee.Image:
    return img.normalizedDifference(["B8", "B4"]).rename("NDVI")


def _ndmi(img: ee.Image) -> ee.Image:
    return img.normalizedDifference(["B8", "B11"]).rename("NDMI")


def _ndti(img: ee.Image) -> ee.Image:
    return img.normalizedDifference(["B4", "B3"]).rename("NDTI")


def _ndci(img: ee.Image) -> ee.Image:
    return img.normalizedDifference(["B5", "B4"]).rename("NDCI")


def _bsi(img: ee.Image) -> ee.Image:
    swir = img.select("B11")
    red = img.select("B4")
    nir = img.select("B8")
    blue = img.select("B2")
    return (
        swir.add(red)
        .subtract(nir.add(blue))
        .divide(swir.add(red).add(nir).add(blue))
        .rename("BSI")
    )


def _fai(img: ee.Image) -> ee.Image:
    red = img.select("B4")
    nir = img.select("B8")
    swir = img.select("B11")
    baseline = red.add(swir.subtract(red).multiply((833 - 665) / (1610 - 665)))
    return nir.subtract(baseline).rename("FAI")


def _percentile_stretch(
    image: ee.Image, region: ee.Geometry, scale: float
) -> ee.Image:
    stats = image.reduceRegion(
        reducer=ee.Reducer.percentile(
            [PERCENTILE_LOW, PERCENTILE_HIGH], ["low", "high"]
        ),
        geometry=region,
        scale=scale,
        maxPixels=1e9,
        tileScale=REDUCE_TILE_SCALE,
        bestEffort=True,
    ).getInfo() or {}
    band = image.bandNames().get(0).getInfo()
    low = _safe_get(stats, f"{band}_low", 0.0)
    high = _safe_get(stats, f"{band}_high", 1.0)
    denom = max(high - low, 1e-6)
    return image.subtract(low).divide(denom).clamp(0, 1)


def _percentile_stretch_batch(
    multiband: ee.Image, region: ee.Geometry, scale: float
) -> ee.Image:
    stats = multiband.reduceRegion(
        reducer=ee.Reducer.percentile(
            [PERCENTILE_LOW, PERCENTILE_HIGH], ["low", "high"]
        ),
        geometry=region,
        scale=scale,
        maxPixels=1e9,
        tileScale=REDUCE_TILE_SCALE,
        bestEffort=True,
    ).getInfo() or {}
    band_names = multiband.bandNames().getInfo()
    stretched = []
    for band in band_names:
        band_img = multiband.select([band])
        low = _safe_get(stats, f"{band}_low", 0.0)
        high = _safe_get(stats, f"{band}_high", 1.0)
        denom = max(high - low, 1e-6)
        stretched.append(band_img.subtract(low).divide(denom).clamp(0, 1))
    out = stretched[0]
    for s in stretched[1:]:
        out = out.addBands(s)
    return out.rename(band_names)


def _seasonal_water_from_collection(
    collection: ee.ImageCollection, start: str, end: str
) -> ee.Image:
    img = collection.filterDate(start, end).median()
    water = _mndwi(img).gt(MNDWI_WATER_THRESHOLD).And(
        _ndvi(img).lt(NDVI_WATER_MAX)
    )
    return water.rename("water").toFloat().set("date", start)


def _seasonal_water_dry_window(
    collection: ee.ImageCollection, start: str, end: str
) -> ee.Image:
    """
    Dry-window persistence sample. For 2026, if that window is empty,
    fall back to the matching 2025 Mar–May window.
    """
    window_coll = collection.filterDate(start, end)
    if start.startswith("2026") and int(window_coll.size().getInfo() or 0) == 0:
        return _seasonal_water_from_collection(
            collection, "2025-03-01", "2025-05-15"
        ).set("date", start)
    return _seasonal_water_from_collection(collection, start, end)


def _fc_from_points(
    points: List[Tuple[str, float, float]],
) -> ee.FeatureCollection:
    feats = [
        ee.Feature(ee.Geometry.Point([lon, lat]), {"name": name})
        for name, lon, lat in points
    ]
    return ee.FeatureCollection(feats)


def _point_inside_aoi(lon: float, lat: float, aoi: ee.Geometry) -> bool:
    """True if point lies in the KML (+ 60 m tolerance for thin corridors)."""
    try:
        pt = ee.Geometry.Point([lon, lat])
        return bool(aoi.buffer(BUFFER_METERS).contains(pt, 1).getInfo())
    except Exception:
        return False


def _extract_hotspot_points_in_kml(
    robust_hotspot: ee.Image,
    confidence_adjusted: ee.Image,
    aoi: ee.Geometry,
    *,
    max_points: int = 50,
) -> List[Dict[str, Any]]:
    """
    Fishing points from robust-hotspot pixels *inside the uploaded KML*.

    Uses reduceToVectors on the hotspot mask clipped to the input AOI, then
    centroids of those patches (not full 250 m grid cells that can sit outside
    a thin river corridor).
    """
    hotspot_in_aoi = robust_hotspot.selfMask().clip(aoi)
    try:
        vectors = hotspot_in_aoi.reduceToVectors(
            geometry=aoi,
            scale=ANALYSIS_SCALE,
            geometryType="polygon",
            eightConnected=True,
            labelProperty="hotspot",
            maxPixels=1e9,
            bestEffort=True,
            tileScale=REDUCE_TILE_SCALE,
        )
        n_vec = int(vectors.size().getInfo() or 0)
    except Exception:
        return []
    if n_vec == 0:
        return []

    # Attach mean suitability and true centroid inside AOI
    def _with_centroid(feat: ee.Feature) -> ee.Feature:
        geom = feat.geometry().intersection(aoi, 1)
        # Thin corridors: keep centroid within buffered KML
        centroid = geom.centroid(1)
        coords = centroid.coordinates()
        mean_suit = confidence_adjusted.reduceRegion(
            reducer=ee.Reducer.mean(),
            geometry=geom,
            scale=ANALYSIS_SCALE,
            maxPixels=1e6,
            bestEffort=True,
            tileScale=REDUCE_TILE_SCALE,
        ).get("confidence_adjusted_suitability")
        return ee.Feature(
            centroid,
            {
                "longitude": coords.get(0),
                "latitude": coords.get(1),
                "fishing_suitability": mean_suit,
            },
        )

    centroids = vectors.map(_with_centroid).filter(
        ee.Filter.notNull(["longitude", "latitude"])
    )
    ranked = centroids.sort("fishing_suitability", False).limit(max_points)
    feats = ranked.getInfo().get("features") or []

    points: List[Dict[str, Any]] = []
    for i, feat in enumerate(feats, start=1):
        props = feat.get("properties") or {}
        lon = props.get("longitude")
        lat = props.get("latitude")
        if lon is None or lat is None:
            g = feat.get("geometry") or {}
            c = g.get("coordinates") or []
            if len(c) >= 2:
                lon, lat = c[0], c[1]
        if lon is None or lat is None:
            continue
        lon_f, lat_f = float(lon), float(lat)
        if not _point_inside_aoi(lon_f, lat_f, aoi):
            continue
        score = props.get("fishing_suitability")
        points.append(
            {
                "name": f"Fishing point {i}",
                "latitude": round(lat_f, 6),
                "longitude": round(lon_f, 6),
                "fishing_suitability": (
                    round(float(score), 4) if score is not None else None
                ),
            }
        )
    return points


def _sample_suitability_points_in_kml(
    confidence_adjusted: ee.Image,
    aoi: ee.Geometry,
    *,
    max_points: int = 15,
) -> List[Dict[str, Any]]:
    """Fallback: sample highest-suitability pixels strictly inside the KML."""
    samples = (
        confidence_adjusted.rename("fishing_suitability")
        .clip(aoi)
        .sample(
            region=aoi,
            scale=ANALYSIS_SCALE,
            numPixels=max(max_points * 20, 100),
            seed=42,
            geometries=True,
            dropNulls=True,
            tileScale=REDUCE_TILE_SCALE,
        )
        .sort("fishing_suitability", False)
        .limit(max_points)
    )
    feats = samples.getInfo().get("features") or []
    points: List[Dict[str, Any]] = []
    for i, feat in enumerate(feats, start=1):
        props = feat.get("properties") or {}
        g = feat.get("geometry") or {}
        c = g.get("coordinates") or []
        if len(c) < 2:
            continue
        lon_f, lat_f = float(c[0]), float(c[1])
        if not _point_inside_aoi(lon_f, lat_f, aoi):
            continue
        score = props.get("fishing_suitability")
        points.append(
            {
                "name": f"Fishing point {i}",
                "latitude": round(lat_f, 6),
                "longitude": round(lon_f, 6),
                "fishing_suitability": (
                    round(float(score), 4) if score is not None else None
                ),
            }
        )
    return points


def _build_fishing_kml(
    points: List[Dict[str, Any]],
    *,
    title: str = "Fishing Points",
) -> bytes:
    """KML placemarks with fish icon for each robust hotspot."""
    root = ET.Element(f"{{{KML_NS}}}kml")
    doc = ET.SubElement(root, f"{{{KML_NS}}}Document")
    ET.SubElement(doc, f"{{{KML_NS}}}name").text = title
    ET.SubElement(doc, f"{{{KML_NS}}}description").text = (
        "Robust fishing suitability hotspots (remote-sensing proxy). "
        "Not a measure of fish abundance or water quality."
    )

    style = ET.SubElement(doc, f"{{{KML_NS}}}Style")
    style.set("id", "fishIcon")
    icon_style = ET.SubElement(style, f"{{{KML_NS}}}IconStyle")
    ET.SubElement(icon_style, f"{{{KML_NS}}}scale").text = "1.2"
    icon = ET.SubElement(icon_style, f"{{{KML_NS}}}Icon")
    ET.SubElement(icon, f"{{{KML_NS}}}href").text = FISH_ICON_HREF
    label_style = ET.SubElement(style, f"{{{KML_NS}}}LabelStyle")
    ET.SubElement(label_style, f"{{{KML_NS}}}scale").text = "0.8"

    for i, pt in enumerate(points, start=1):
        lon = pt["longitude"]
        lat = pt["latitude"]
        score = pt.get("fishing_suitability")
        name = pt.get("name") or f"Fishing point {i}"
        pm = ET.SubElement(doc, f"{{{KML_NS}}}Placemark")
        ET.SubElement(pm, f"{{{KML_NS}}}name").text = str(name)
        desc_parts = [f"Lat: {lat}", f"Lon: {lon}"]
        if score is not None:
            desc_parts.append(f"Suitability: {score}")
        ET.SubElement(pm, f"{{{KML_NS}}}description").text = "\n".join(desc_parts)
        ET.SubElement(pm, f"{{{KML_NS}}}styleUrl").text = "#fishIcon"
        point = ET.SubElement(pm, f"{{{KML_NS}}}Point")
        ET.SubElement(point, f"{{{KML_NS}}}coordinates").text = f"{lon},{lat},0"

    xml_bytes = ET.tostring(root, encoding="utf-8")
    return minidom.parseString(xml_bytes).toprettyxml(indent="  ", encoding="utf-8")


def analyze_fishing_points(
    aoi_buffered: ee.Geometry,
    aoi: ee.Geometry,
    *,
    access_points: Optional[List[Tuple[str, float, float]]] = None,
    aoi_info: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Run fishing suitability → robust hotspots → lat/lon points + one KML.

    Dry: Mar–Apr 2026; wet: Jul–Sep 2026.
    For 2026 dry Mar–Apr, only one Sentinel-2 image is used.
    """
    access_points = access_points or _sample_access_along_aoi(aoi)

    # ---- S2 composites ----
    dry_masked = _get_masked_collection(DRY_WINDOWS, aoi_buffered)
    dry_masked, dry_2026_note = _limit_2026_dry_to_one_image(
        dry_masked, aoi_buffered
    )
    wet_masked = _get_masked_collection(WET_WINDOWS, aoi_buffered)

    dry_count = int(dry_masked.size().getInfo() or 0)
    wet_count = int(wet_masked.size().getInfo() or 0)
    if dry_count == 0:
        raise ValueError(
            "No cloud-masked Sentinel-2 scenes found in the dry window "
            "(Mar–Apr 2026) over the KML area."
        )
    if wet_count == 0:
        raise ValueError(
            "No cloud-masked Sentinel-2 scenes found in the wet window "
            "(Jul–Sep 2026) over the KML area."
        )

    dry_composite = dry_masked.median().clip(aoi_buffered)
    wet_composite = wet_masked.median().clip(aoi_buffered)
    dry_clear_count = dry_masked.select("B2").count().clip(aoi_buffered)

    # ---- SAR wet support ----
    s1 = (
        ee.ImageCollection("COPERNICUS/S1_GRD")
        .filterBounds(aoi_buffered)
        .filter(ee.Filter.eq("instrumentMode", "IW"))
        .filter(ee.Filter.listContains("transmitterReceiverPolarisation", "VH"))
        .select("VH")
        .filter(_build_date_filter(WET_WINDOWS))
    )
    s1_wet = (
        s1.median()
        .clip(aoi_buffered)
        .focal_median(SAR_SPECKLE_RADIUS, "circle", "pixels")
    )
    sar_water = s1_wet.lt(SAR_VH_THRESHOLD).rename("sar_water")

    dry_mndwi = _mndwi(dry_composite)
    wet_mndwi = _mndwi(wet_composite)
    dry_ndvi = _ndvi(dry_composite)
    wet_ndvi = _ndvi(wet_composite)

    dry_optical = dry_mndwi.gt(MNDWI_WATER_THRESHOLD).And(
        dry_ndvi.lt(NDVI_WATER_MAX)
    )
    wet_optical = wet_mndwi.gt(MNDWI_WATER_THRESHOLD).And(
        wet_ndvi.lt(NDVI_WATER_MAX)
    )
    wet_water = wet_optical.Or(sar_water).rename("wet_water")
    dry_water = dry_optical.rename("dry_water")

    # ---- Persistence ----
    persistence_images = []
    for w in DRY_WINDOWS:
        persistence_images.append(
            _seasonal_water_dry_window(dry_masked, w[0], w[1])
        )
    for w in WET_WINDOWS:
        persistence_images.append(
            _seasonal_water_from_collection(wet_masked, w[0], w[1])
        )
    persistence_coll = ee.ImageCollection.fromImages(persistence_images)
    optical_persistence = persistence_coll.mean().rename("water_persistence")
    n_opt = len(persistence_images)
    sar_w = 1 / (n_opt + 1)
    opt_w = n_opt / (n_opt + 1)
    water_persistence = (
        optical_persistence.multiply(opt_w)
        .add(sar_water.toFloat().multiply(sar_w))
        .clamp(0, 1)
        .rename("water_persistence")
    )

    water_stability = (
        water_persistence.multiply(0.7)
        .add(dry_water.And(wet_water).toFloat().multiply(0.3))
        .clamp(0, 1)
        .rename("water_stability")
    )
    seasonal_water = wet_water.And(dry_water.Not()).rename("seasonal_water")

    # ---- DEM / accessibility ----
    dem = (
        ee.ImageCollection("COPERNICUS/DEM/GLO30")
        .select("DEM")
        .mosaic()
        .clip(aoi_buffered)
    )
    slope = ee.Terrain.slope(dem).rename("slope_degrees")
    accessibility_slope = (
        ee.Image(1)
        .subtract(
            slope.subtract(SLOPE_IDEAL)
            .max(0)
            .divide(SLOPE_MAX - SLOPE_IDEAL)
        )
        .clamp(0, 1)
    )
    ndmi = _ndmi(dry_composite)
    moisture_penalty = (
        ndmi.subtract(HIGH_MOISTURE_NDMI).max(0).divide(0.4).clamp(0, 1)
    )
    bank_accessibility = (
        accessibility_slope.multiply(
            ee.Image(1).subtract(moisture_penalty.multiply(0.5))
        )
        .updateMask(dry_water.Not())
        .rename("bank_accessibility")
    )
    dem_validity = dem.mask().reduceNeighborhood(
        reducer=ee.Reducer.mean(),
        kernel=ee.Kernel.circle(radius=3, units="pixels"),
    ).rename("dem_validity")

    # ---- Dynamic World habitat ----
    dynamic_world = (
        ee.ImageCollection("GOOGLE/DYNAMICWORLD/V1")
        .filterBounds(aoi_buffered)
        .filterDate("2024-01-01", "2026-06-01")
        .select("label")
        .mode()
        .clip(aoi_buffered)
    )
    natural_riparian = (
        dynamic_world.eq(1)
        .Or(dynamic_world.eq(2))
        .Or(dynamic_world.eq(3))
        .Or(dynamic_world.eq(5))
    )
    bare_land = dynamic_world.eq(7)
    built_land = dynamic_world.eq(6)
    habitat_base = (
        natural_riparian.multiply(1.0)
        .add(bare_land.multiply(0.35))
        .subtract(built_land.multiply(0.25))
        .clamp(0, 1)
    )
    bsi = _bsi(dry_composite)
    bare_spectral = bsi.gt(0.1)
    spectral_agreement = natural_riparian.And(bare_spectral.Not()).Or(
        bare_land.And(bare_spectral)
    )
    habitat_confidence = habitat_base.multiply(
        ee.Image(0.75).add(spectral_agreement.multiply(0.25))
    ).rename("habitat_score")

    # ---- Morphology ----
    channel_width_proxy = water_persistence.reduceNeighborhood(
        reducer=ee.Reducer.mean(),
        kernel=ee.Kernel.circle(radius=5, units="pixels"),
    ).rename("channel_width_proxy")
    water_edge = (
        water_persistence.gt(0.5)
        .focal_max(2)
        .subtract(water_persistence.gt(0.5).focal_min(2))
        .abs()
        .rename("channel_edge_complexity")
    )
    morphology_score = (
        channel_width_proxy.multiply(0.6)
        .add(water_edge.multiply(0.4))
        .clamp(0, 1)
        .rename("morphology_score")
    )

    # ---- Access / estuary / bridges from KML points ----
    access_fc = _fc_from_points(access_points)
    # Downstream-most point as estuary/outlet proxy (lowest lat heuristic, else last)
    estuary_pt = min(access_points, key=lambda p: p[2])
    estuary_fc = _fc_from_points(
        [(f"{estuary_pt[0]} (outlet proxy)", estuary_pt[1], estuary_pt[2])]
    )
    bridge_pts = [
        p
        for p in access_points
        if "bridge" in p[0].lower() or "crossing" in p[0].lower()
    ]
    if not bridge_pts:
        bridge_pts = access_points
    bridge_fc = _fc_from_points(bridge_pts)

    distance_access = access_fc.distance(searchRadius=3000, maxError=10).clip(
        aoi_buffered
    )
    access_proximity = (
        ee.Image(1)
        .subtract(distance_access.divide(ACCESS_DECAY_M))
        .clamp(0, 1)
        .rename("access_proximity")
    )
    distance_estuary = estuary_fc.distance(searchRadius=3000, maxError=10).clip(
        aoi_buffered
    )
    estuary_mouth_score = (
        ee.Image(1)
        .subtract(distance_estuary.divide(ESTUARY_DECAY_M))
        .clamp(0, 1)
        .rename("estuary_mouth_proximity")
    )
    distance_bridge = bridge_fc.distance(searchRadius=3000, maxError=10).clip(
        aoi_buffered
    )
    bridge_score = (
        ee.Image(1)
        .subtract(distance_bridge.divide(BRIDGE_DECAY_M))
        .clamp(0, 1)
        .rename("bridge_proximity")
    )
    location_access_score = (
        bank_accessibility.unmask(0)
        .multiply(0.6)
        .add(access_proximity.multiply(0.4))
        .clamp(0, 1)
        .rename("location_access_score")
    )

    # ---- Stress layers (batched percentile stretch) ----
    ndti = _ndti(dry_composite).updateMask(dry_water)
    ndci = _ndci(dry_composite).updateMask(dry_water)
    fai = _fai(dry_composite).updateMask(dry_water)

    chirps = (
        ee.ImageCollection("UCSB-CHG/CHIRPS/DAILY")
        .filterBounds(aoi_buffered)
        .select("precipitation")
    )

    def _antecedent_rainfall(windows: List[List[str]], days: int) -> ee.Image:
        sums = []
        for w in windows:
            end_date = ee.Date(w[1])
            start_date = end_date.advance(-days, "day")
            sums.append(chirps.filterDate(start_date, end_date).sum())
        return ee.ImageCollection.fromImages(sums).mean()

    recent_rain = (
        _antecedent_rainfall(DRY_WINDOWS, RAINFALL_ANTECEDENT_DAYS)
        .clip(aoi_buffered)
        .rename("recent_rainfall_mm")
    )
    stress_inputs = ndti.addBands(ndci).addBands(fai).addBands(recent_rain)
    stretched = _percentile_stretch_batch(
        stress_inputs, aoi_buffered, ANALYSIS_SCALE
    )
    turbidity_stress = stretched.select("NDTI").rename("turbidity_stress")
    ndci_stress = stretched.select("NDCI")
    fai_stress = stretched.select("FAI")
    high_rain_ctx = stretched.select("recent_rainfall_mm").rename(
        "high_rainfall_context"
    )
    algal_stress = (
        ndci_stress.multiply(0.6)
        .add(fai_stress.multiply(0.4))
        .clamp(0, 1)
        .rename("algal_stress")
    )

    # ---- Landsat thermal ----
    landsat = (
        ee.ImageCollection("LANDSAT/LC09/C02/T1_L2")
        .merge(ee.ImageCollection("LANDSAT/LC08/C02/T1_L2"))
        .filterBounds(aoi_buffered)
        .filter(_build_date_filter(DRY_WINDOWS))
        .filter(ee.Filter.lt("CLOUD_COVER", 30))
    )

    def _scale_thermal(img: ee.Image) -> ee.Image:
        return (
            img.select("ST_B10")
            .multiply(0.00341802)
            .add(149.0)
            .subtract(273.15)
            .rename("LST_C")
            .copyProperties(img, ["system:time_start"])
        )

    lst = landsat.map(_scale_thermal).median().clip(aoi_buffered)
    water_temperature = lst.updateMask(dry_water)
    thermal_mean = water_temperature.reduceNeighborhood(
        reducer=ee.Reducer.mean(),
        kernel=ee.Kernel.circle(radius=4, units="pixels"),
    )
    thermal_anomaly = water_temperature.subtract(thermal_mean).gt(
        THERMAL_ANOMALY_C
    ).rename("thermal_anomaly")
    thermal_stress = thermal_anomaly.toFloat().rename("thermal_stress")

    turbidity_local_mean = ndti.reduceNeighborhood(
        reducer=ee.Reducer.mean(),
        kernel=ee.Kernel.circle(radius=4, units="pixels"),
    )
    turbidity_anomaly = ndti.subtract(turbidity_local_mean).gt(
        TURBIDITY_ANOMALY_THRESHOLD
    ).rename("turbidity_anomaly")
    discharge_medium = thermal_anomaly.Or(turbidity_anomaly).rename(
        "medium_confidence_discharge_candidate"
    )
    discharge_signal_adjusted = (
        discharge_medium.toFloat()
        .multiply(ee.Image(1).subtract(high_rain_ctx.multiply(0.5)))
        .rename("discharge_signal_adjusted")
    )

    # ---- Anthropogenic ----
    population = (
        ee.ImageCollection("WorldPop/GP/100m/pop")
        .filterBounds(aoi_buffered)
        .filterDate("2015-01-01", "2021-01-01")
        .sort("system:time_start", False)
        .mosaic()
        .clip(aoi_buffered)
        .rename("population")
    )
    population_pressure = _percentile_stretch(population, aoi_buffered, 100)
    built_pressure = built_land.toFloat().rename("built_pressure")
    anthropogenic_pressure = (
        population_pressure.multiply(0.65)
        .add(built_pressure.multiply(0.35))
        .clamp(0, 1)
        .rename("anthropogenic_pressure")
    )

    pollution_stress = (
        turbidity_stress.unmask(0)
        .multiply(POLLUTION_WEIGHTS["turbidity"])
        .add(algal_stress.unmask(0).multiply(POLLUTION_WEIGHTS["algalStress"]))
        .add(thermal_stress.unmask(0).multiply(POLLUTION_WEIGHTS["thermalStress"]))
        .add(
            anthropogenic_pressure.unmask(0).multiply(
                POLLUTION_WEIGHTS["anthropogenic"]
            )
        )
        .add(
            discharge_signal_adjusted.unmask(0).multiply(
                POLLUTION_WEIGHTS["dischargeSignal"]
            )
        )
        .clamp(0, 1)
        .rename("pollution_stress")
    )

    # ---- Fishing suitability modules ----
    persistence_score = water_persistence.rename("water_persistence_score")
    stability_score = water_stability.rename("water_stability_score")
    habitat_score = habitat_confidence.rename("habitat_score")
    accessibility_score = bank_accessibility.rename("accessibility_score")
    morphology_score_final = morphology_score.rename("morphology_score")
    seasonal_score = (
        seasonal_water.toFloat()
        .multiply(0.7)
        .add(water_persistence.multiply(0.3))
        .clamp(0, 1)
        .rename("seasonal_water_score")
    )
    location_score = (
        location_access_score.multiply(0.6)
        .add(estuary_mouth_score.multiply(0.2))
        .add(bridge_score.multiply(0.2))
        .clamp(0, 1)
        .rename("location_score")
    )

    base_fishing = (
        persistence_score.unmask(0)
        .multiply(FISHING_WEIGHTS["waterPersistence"])
        .add(stability_score.unmask(0).multiply(FISHING_WEIGHTS["waterStability"]))
        .add(habitat_score.unmask(0).multiply(FISHING_WEIGHTS["habitat"]))
        .add(
            accessibility_score.unmask(0).multiply(FISHING_WEIGHTS["accessibility"])
        )
        .add(
            morphology_score_final.unmask(0).multiply(FISHING_WEIGHTS["morphology"])
        )
        .add(seasonal_score.unmask(0).multiply(FISHING_WEIGHTS["seasonalWater"]))
        .add(location_score.unmask(0).multiply(FISHING_WEIGHTS["locationAccess"]))
        .clamp(0, 1)
        .rename("base_fishing_suitability")
    )
    pollution_penalty = pollution_stress.multiply(0.30)
    fishing_suitability = (
        base_fishing.multiply(ee.Image(1).subtract(pollution_penalty))
        .clamp(0, 1)
        .rename("fishing_suitability")
    )

    # ---- Data confidence ----
    observation_confidence = dry_clear_count.divide(EXPECTED_CLEAR_OBS).clamp(0, 1)
    sensor_agreement = wet_optical.eq(sar_water).rename("sensor_agreement")
    data_confidence = (
        observation_confidence.multiply(CONFIDENCE_WEIGHTS["observation"])
        .add(
            sensor_agreement.toFloat().multiply(
                CONFIDENCE_WEIGHTS["sensorAgreement"]
            )
        )
        .add(dem_validity.multiply(CONFIDENCE_WEIGHTS["demValidity"]))
        .clamp(0, 1)
        .rename("data_confidence")
    )
    confidence_adjusted = (
        fishing_suitability.multiply(data_confidence.multiply(0.5).add(0.5))
        .clamp(0, 1)
        .rename("confidence_adjusted_suitability")
    )

    # ---- Monte Carlo (band-axis reduction) ----
    if ENABLE_MONTE_CARLO:
        mc_images = []
        rng = random.Random(42)
        for i in range(MONTE_CARLO_ITERATIONS):
            raw = [rng.random() for _ in range(7)]
            total = sum(raw) or 1.0
            nrm = [v / total for v in raw]
            mc_score = (
                persistence_score.unmask(0)
                .multiply(nrm[0])
                .add(stability_score.unmask(0).multiply(nrm[1]))
                .add(habitat_score.unmask(0).multiply(nrm[2]))
                .add(accessibility_score.unmask(0).multiply(nrm[3]))
                .add(morphology_score_final.unmask(0).multiply(nrm[4]))
                .add(seasonal_score.unmask(0).multiply(nrm[5]))
                .add(location_score.unmask(0).multiply(nrm[6]))
                .clamp(0, 1)
                .rename(f"mc_{i}")
            )
            mc_images.append(mc_score)
        mc_bands = ee.Image.cat(mc_images)
        mc_mean = mc_bands.reduce(ee.Reducer.mean()).rename("mc_mean")
        mc_std = mc_bands.reduce(ee.Reducer.stdDev()).rename("mc_stddev")
    else:
        mc_mean = fishing_suitability
        mc_std = ee.Image(0).rename("mc_stddev")

    # ---- Scenario agreement (batched p80) ----
    scenario_a = (
        persistence_score.multiply(0.25)
        .add(habitat_score.multiply(0.20))
        .add(accessibility_score.multiply(0.20))
        .add(stability_score.multiply(0.15))
        .add(morphology_score_final.multiply(0.10))
        .add(location_score.multiply(0.10))
        .rename("scenario_A")
    )
    scenario_b = (
        persistence_score.multiply(0.15)
        .add(habitat_score.multiply(0.25))
        .add(accessibility_score.multiply(0.15))
        .add(stability_score.multiply(0.20))
        .add(morphology_score_final.multiply(0.15))
        .add(location_score.multiply(0.10))
        .rename("scenario_B")
    )
    scenario_c = (
        persistence_score.multiply(0.20)
        .add(habitat_score.multiply(0.15))
        .add(accessibility_score.multiply(0.25))
        .add(stability_score.multiply(0.15))
        .add(morphology_score_final.multiply(0.15))
        .add(location_score.multiply(0.10))
        .rename("scenario_C")
    )
    scenario_stack = scenario_a.addBands(scenario_b).addBands(scenario_c)
    scenario_thresholds = scenario_stack.reduceRegion(
        reducer=ee.Reducer.percentile([80], ["p80"]),
        geometry=aoi,
        scale=ANALYSIS_SCALE,
        maxPixels=1e9,
        tileScale=REDUCE_TILE_SCALE,
        bestEffort=True,
    ).getInfo() or {}
    sa = _safe_get(scenario_thresholds, "scenario_A_p80", 1.0)
    sb = _safe_get(scenario_thresholds, "scenario_B_p80", 1.0)
    sc = _safe_get(scenario_thresholds, "scenario_C_p80", 1.0)
    agreement = (
        scenario_a.gte(sa)
        .add(scenario_b.gte(sb))
        .add(scenario_c.gte(sc))
        .rename("scenario_agreement")
    )

    robust_hotspot = (
        confidence_adjusted.gte(0.70)
        .And(data_confidence.gte(0.60))
        .And(agreement.gte(2))
        .And(mc_std.lt(0.15))
        .rename("robust_hotspot")
    )

    # ---- Points inside uploaded KML (not 250 m grid cells outside corridor) ----
    fishing_points = _extract_hotspot_points_in_kml(
        robust_hotspot, confidence_adjusted, aoi
    )
    if not fishing_points:
        # Soften: high suitability + confidence inside KML if robust mask empty
        soft_hotspot = (
            confidence_adjusted.gte(0.55)
            .And(data_confidence.gte(0.40))
            .rename("robust_hotspot")
        )
        fishing_points = _extract_hotspot_points_in_kml(
            soft_hotspot, confidence_adjusted, aoi
        )
    if not fishing_points:
        fishing_points = _sample_suitability_points_in_kml(
            confidence_adjusted, aoi
        )

    if not fishing_points:
        raise ValueError(
            "No fishing hotspot points found inside the uploaded KML for the "
            "configured dry/wet windows."
        )

    kml_bytes = _build_fishing_kml(
        fishing_points, title="Fishing Points — Robust Hotspots"
    )

    result: Dict[str, Any] = {
        "point_count": len(fishing_points),
        "fishing_points": [
            {"latitude": p["latitude"], "longitude": p["longitude"]}
            for p in fishing_points
        ],
        "dry_windows": DRY_WINDOWS,
        "wet_windows": WET_WINDOWS,
        "sentinel2_dry_count": dry_count,
        "sentinel2_wet_count": wet_count,
        "notes": {
            "proxy": (
                "Remote-sensing suitability/proxy model — not fish abundance, "
                "catch, DO, BOD/COD, or real fishing activity."
            ),
            "dry_2026": dry_2026_note,
            "buffer_m": BUFFER_METERS,
            "points": "Lat/lon are centroids of hotspot patches inside the uploaded KML.",
            "output": "One KML with fish-icon placemarks + lat/lon points only.",
        },
        "kml_bytes": kml_bytes,
        "kml_filename": "fishing_points.kml",
    }
    if aoi_info:
        result["aoi"] = aoi_info
    return result
