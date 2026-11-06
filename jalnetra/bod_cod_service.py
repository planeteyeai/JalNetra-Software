"""
jalnetra.bod_cod_service — Main-stream BOD/COD from Landsat 8/9 + Sentinel-2.

Python port of the Mandvi joining-streams GEE workflow:

  BOD : Landsat 8+9 TOA → Wang BOD → fixed local calibration
  COD : BOD × ratio + Sentinel-2 correction (TSS, chlorophyll)

Outputs daily BOD/COD at 10 m chainage along the uploaded stream KML,
plus a pixel KML (30 m squares) for the period median composite.
"""
from __future__ import annotations

import math
import xml.etree.ElementTree as ET
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from xml.dom import minidom

import ee
from pyproj import Transformer
from shapely.geometry import LineString, MultiLineString
from shapely.ops import linemerge, transform as shp_transform, unary_union

from jalnetra.kml_buffer import _geom_to_ee, _utm_epsg
from jalnetra.scene_dates import recent_window

KML_NS = "http://www.opengis.net/kml/2.2"

# Half-width around stream lines (~3 Landsat pixels). JS comment documents 45 m.
STREAM_BUFFER_M = 45.0
CHAINAGE_STEP_M = 10.0
LANDSAT_SCALE_M = 30
MAX_CLOUD = 20
MAX_CLOUD_S2 = 30

BOD_SLOPE = 4.641372171364311
BOD_INTERCEPT = -20.97234943238684

COD_BOD_RATIO = 2.0
COD_TSS_W = 8.0
COD_CHL_W = 6.0
COD_SLOPE = 1.0
COD_INTERCEPT = 0.0

USE_WATER_MASK = True
USE_DYNAMIC_WORLD = True
DW_WATER_THRESHOLD = 0.30
# Matches GEE script: MNDWI.gt(0.25).and(B5.lt(0.18)).and(B6.lt(0.15))
MNDWI_MIN = 0.25

PALETTE = ["006400", "7FFF00", "FFFF00", "FFA500", "FF0000"]
# Earth Engine aborts client getInfo() after ~5000 features.
EE_GETINFO_CHUNK = 4000
CHAINAGE_SAMPLE_CHUNK = 1500
PIXEL_KML_MAX = 4000


def _local_name(tag: str) -> str:
    return tag.split("}")[-1] if "}" in tag else tag


def _parse_coords(coord_text: str) -> List[Tuple[float, float]]:
    pts: List[Tuple[float, float]] = []
    for token in coord_text.strip().split():
        parts = token.split(",")
        if len(parts) >= 2:
            pts.append((float(parts[0]), float(parts[1])))
    return pts


def _placemark_name(pm: ET.Element, fallback: str) -> str:
    for child in list(pm):
        if _local_name(child.tag) == "name" and child.text and child.text.strip():
            return child.text.strip()
    return fallback


def _placemark_prop(pm: ET.Element, key: str) -> Optional[str]:
    """Read ExtendedData / SimpleData / Data value by name (e.g. chainage)."""
    key_l = key.lower()
    for elem in pm.iter():
        tag = _local_name(elem.tag)
        if tag == "SimpleData" and (elem.get("name") or "").lower() == key_l:
            if elem.text and elem.text.strip():
                return elem.text.strip()
        if tag == "Data" and (elem.get("name") or "").lower() == key_l:
            for child in list(elem):
                if _local_name(child.tag) == "value" and child.text:
                    return child.text.strip()
    return None


def _lines_from_placemark(pm: ET.Element) -> List[LineString]:
    lines: List[LineString] = []
    for elem in pm.iter():
        if _local_name(elem.tag) != "LineString":
            continue
        for coords_elem in elem.iter():
            if _local_name(coords_elem.tag) != "coordinates":
                continue
            if not coords_elem.text or not coords_elem.text.strip():
                continue
            pts = _parse_coords(coords_elem.text)
            if len(pts) >= 2:
                lines.append(LineString(pts))
    # Polygon centreline fallback: use exterior ring as a closed path.
    if not lines:
        for outer in pm.iter():
            if _local_name(outer.tag) != "outerBoundaryIs":
                continue
            for coords_elem in outer.iter():
                if _local_name(coords_elem.tag) != "coordinates":
                    continue
                if not coords_elem.text or not coords_elem.text.strip():
                    continue
                ring = _parse_coords(coords_elem.text)
                if len(ring) >= 2:
                    lines.append(LineString(ring))
    return lines


def parse_stream_features(kml_bytes: bytes) -> List[Dict[str, Any]]:
    """Named stream LineStrings from KML placemarks."""
    try:
        root = ET.fromstring(kml_bytes)
    except ET.ParseError as exc:
        raise ValueError(f"Invalid KML file: {exc}") from exc

    features: List[Dict[str, Any]] = []
    for pm in root.iter():
        if _local_name(pm.tag) != "Placemark":
            continue
        lines = _lines_from_placemark(pm)
        if not lines:
            continue
        geom = lines[0] if len(lines) == 1 else linemerge(lines)
        if isinstance(geom, MultiLineString):
            geom = max(geom.geoms, key=lambda g: g.length)
        if geom.is_empty or geom.length == 0:
            continue
        idx = len(features) + 1
        name = _placemark_name(pm, f"Stream {idx}")
        features.append(
            {
                "stream_id": f"S{idx:02d}",
                "stream_name": name,
                "chainage_label": _placemark_prop(pm, "chainage"),
                "geometry": geom,
            }
        )

    if not features:
        raise ValueError(
            "No stream LineString/Polygon placemarks found in KML."
        )
    return features


def _projectors(lon: float, lat: float):
    epsg = _utm_epsg(lon, lat)
    to_utm = Transformer.from_crs("EPSG:4326", f"EPSG:{epsg}", always_xy=True)
    to_wgs = Transformer.from_crs(f"EPSG:{epsg}", "EPSG:4326", always_xy=True)
    return to_utm, to_wgs


def _chainage_points_for_line(
    line: LineString,
    *,
    stream_id: str,
    stream_name: str,
    step_m: float = CHAINAGE_STEP_M,
) -> List[Dict[str, Any]]:
    """Densify a geographic line into points every `step_m` metres."""
    lon0, lat0 = line.coords[0]
    to_utm, to_wgs = _projectors(lon0, lat0)
    utm_line = shp_transform(lambda x, y: to_utm.transform(x, y), line)
    length_m = float(utm_line.length)
    if length_m <= 0:
        return []

    n = max(1, int(math.floor(length_m / step_m)) + 1)
    points: List[Dict[str, Any]] = []
    for i in range(n):
        dist = min(i * step_m, length_m)
        pt_utm = utm_line.interpolate(dist)
        lon, lat = to_wgs.transform(pt_utm.x, pt_utm.y)
        points.append(
            {
                "stream_id": stream_id,
                "stream_name": stream_name,
                "chainage_m": round(dist, 1),
                "longitude": round(float(lon), 6),
                "latitude": round(float(lat), 6),
            }
        )
    # Always include the downstream end if not already hit.
    end = utm_line.interpolate(length_m)
    end_lon, end_lat = to_wgs.transform(end.x, end.y)
    if not points or abs(points[-1]["chainage_m"] - length_m) > 0.05:
        points.append(
            {
                "stream_id": stream_id,
                "stream_name": stream_name,
                "chainage_m": round(length_m, 1),
                "longitude": round(float(end_lon), 6),
                "latitude": round(float(end_lat), 6),
            }
        )
    return points


def _mask_landsat(img: ee.Image) -> ee.Image:
    qa = img.select("QA_PIXEL")
    m = (
        qa.bitwiseAnd(1 << 0)
        .eq(0)
        .And(qa.bitwiseAnd(1 << 1).eq(0))
        .And(qa.bitwiseAnd(1 << 2).eq(0))
        .And(qa.bitwiseAnd(1 << 3).eq(0))
        .And(qa.bitwiseAnd(1 << 4).eq(0))
        .And(qa.bitwiseAnd(1 << 5).eq(0))
    )
    return img.updateMask(m).copyProperties(img, img.propertyNames())


def _mask_s2(img: ee.Image) -> ee.Image:
    qa = img.select("QA60")
    m = qa.bitwiseAnd(1 << 10).eq(0).And(qa.bitwiseAnd(1 << 11).eq(0))
    return (
        img.updateMask(m)
        .divide(10000)
        .copyProperties(img, img.propertyNames())
    )


def _get_landsat(aoi: ee.Geometry, start: str, end: str) -> ee.ImageCollection:
    def _col(asset_id: str) -> ee.ImageCollection:
        return (
            ee.ImageCollection(asset_id)
            .filterBounds(aoi)
            .filterDate(start, end)
            .filter(ee.Filter.lt("CLOUD_COVER", MAX_CLOUD))
        )

    return _col("LANDSAT/LC08/C02/T1_TOA").merge(
        _col("LANDSAT/LC09/C02/T1_TOA")
    )


def _water_mask(
    ls_image: ee.Image,
    aoi: ee.Geometry,
    start: str,
    end: str,
) -> ee.Image:
    b3 = ls_image.select("B3")
    b5 = ls_image.select("B5")
    b6 = ls_image.select("B6")
    mndwi = b3.subtract(b6).divide(b3.add(b6))
    initial_water = mndwi.gt(MNDWI_MIN).And(b5.lt(0.18)).And(b6.lt(0.15))
    # Match GEE: connectedPixelCount(100, true).gte(5)
    ls_water = (
        initial_water.And(initial_water.connectedPixelCount(100, True).gte(5))
        .selfMask()
    )

    dw_water = ee.Image(0).selfMask()
    if USE_DYNAMIC_WORLD:
        dw_col = (
            ee.ImageCollection("GOOGLE/DYNAMICWORLD/V1")
            .filterBounds(aoi)
            .filterDate(start, end)
        )
        dw_water = (
            dw_col.select("water").mean().gte(DW_WATER_THRESHOLD).selfMask()
        )
    water_mask = ls_water.unmask(0).Or(dw_water.unmask(0)).selfMask()
    if USE_WATER_MASK:
        return water_mask.rename("Water").clip(aoi)
    return ee.Image(1).rename("Water").clip(aoi)


def _wang_bod(ls_image: ee.Image) -> ee.Image:
    b2 = ls_image.select("B2")
    b3 = ls_image.select("B3")
    b4 = ls_image.select("B4")
    return (
        ee.Image(1.79)
        .subtract(b2.multiply(0.789))
        .add(b3.multiply(52.36))
        .subtract(b4.multiply(3.28))
    )


def _calibrated_bod(ls_image: ee.Image, water: ee.Image) -> ee.Image:
    return (
        _wang_bod(ls_image)
        .multiply(BOD_SLOPE)
        .add(BOD_INTERCEPT)
        .max(0)
        .updateMask(water)
        .rename("BOD_mg_L")
    )


def _s2_correction(
    aoi: ee.Geometry,
    start: str,
    end: str,
    ls_proj: ee.Projection,
) -> Tuple[ee.Image, ee.Image, int]:
    s2_raw = (
        ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
        .filterBounds(aoi)
        .filterDate(start, end)
        .filter(ee.Filter.lte("CLOUDY_PIXEL_PERCENTAGE", MAX_CLOUD_S2))
    )
    s2_count = int(s2_raw.size().getInfo() or 0)
    if s2_count == 0:
        zero = ee.Image(0).rename("TSS_norm").clip(aoi)
        return zero, ee.Image(0).rename("NDCI_norm").clip(aoi), 0

    s2_proj = ee.Image(s2_raw.first()).select("B4").projection()
    s2 = (
        s2_raw.map(_mask_s2)
        .median()
        .setDefaultProjection(s2_proj)
        .clip(aoi)
    )
    tss_10m = s2.select("B4").divide(s2.select("B8")).rename("TSS")
    ndci_10m = s2.normalizedDifference(["B5", "B4"]).rename("NDCI")

    def to_landsat_grid(img: ee.Image) -> ee.Image:
        return (
            img.setDefaultProjection(s2_proj)
            .reduceResolution(reducer=ee.Reducer.mean(), maxPixels=1024)
            .reproject(crs=ls_proj)
        )

    tss_norm = to_landsat_grid(tss_10m).unitScale(0, 5).clamp(0, 1)
    ndci_norm = to_landsat_grid(ndci_10m).unitScale(-1, 1).clamp(0, 1)
    return tss_norm.rename("TSS_norm"), ndci_norm.rename("NDCI_norm"), s2_count


def _cod_from_bod(
    bod: ee.Image,
    tss_norm: ee.Image,
    ndci_norm: ee.Image,
    water: ee.Image,
) -> ee.Image:
    cod_raw = (
        bod.multiply(COD_BOD_RATIO)
        .add(tss_norm.unmask(0).multiply(COD_TSS_W))
        .add(ndci_norm.unmask(0).multiply(COD_CHL_W))
    )
    return (
        cod_raw.multiply(COD_SLOPE)
        .add(COD_INTERCEPT)
        .max(0)
        .updateMask(water)
        .rename("COD_mg_L")
    )


def _dates_of(col: ee.ImageCollection) -> List[str]:
    arr = (
        col.sort("system:time_start")
        .aggregate_array("system:time_start")
        .map(lambda d: ee.Date(d).format("YYYY-MM-dd"))
        .distinct()
        .getInfo()
    )
    return list(arr or [])


def _ee_chainage_fc(points: List[Dict[str, Any]]) -> ee.FeatureCollection:
    feats = []
    for p in points:
        feats.append(
            ee.Feature(
                ee.Geometry.Point([p["longitude"], p["latitude"]]),
                {
                    "stream_id": p["stream_id"],
                    "stream_name": p["stream_name"],
                    "chainage_m": p["chainage_m"],
                    "longitude": p["longitude"],
                    "latitude": p["latitude"],
                },
            )
        )
    return ee.FeatureCollection(feats)


def _fc_features(
    fc: ee.FeatureCollection,
    *,
    chunk: int = EE_GETINFO_CHUNK,
    max_features: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Pull FeatureCollection features in chunks under EE's 5000 getInfo limit."""
    base = fc.limit(int(max_features)) if max_features is not None else fc
    # Fast path for already-small collections (e.g. one chainage chunk).
    try:
        return list(base.getInfo().get("features") or [])
    except ee.EEException as exc:
        if "5000" not in str(exc):
            raise

    out: List[Dict[str, Any]] = []
    start = 0
    while True:
        if max_features is not None and start >= max_features:
            break
        take = chunk
        if max_features is not None:
            take = min(chunk, max_features - start)
        subset = ee.FeatureCollection(base.toList(take, start))
        feats = subset.getInfo().get("features") or []
        out.extend(feats)
        if len(feats) < take:
            break
        start += take
    return out


def _chunks(items: List[Any], size: int) -> List[List[Any]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def _ee_stream_buffers(
    features: List[Dict[str, Any]],
) -> ee.FeatureCollection:
    feats = []
    for f in features:
        geom = _geom_to_ee(f["geometry"]).buffer(STREAM_BUFFER_M)
        feats.append(
            ee.Feature(
                geom,
                {
                    "stream_id": f["stream_id"],
                    "name": f["stream_name"],
                    "chainage": f.get("chainage_label") or "",
                },
            )
        )
    return ee.FeatureCollection(feats)


def _el(parent: ET.Element, tag: str, text: Optional[str] = None) -> ET.Element:
    node = ET.SubElement(parent, f"{{{KML_NS}}}{tag}")
    if text is not None:
        node.text = text
    return node


def _build_pixel_kml(
    pixels: List[Dict[str, Any]],
    *,
    doc_name: str = "Main stream BOD/COD pixels",
) -> bytes:
    """30 m square placemarks with BOD/COD popup (matches GEE KML export)."""
    root = ET.Element("kml", xmlns=KML_NS)
    doc = _el(root, "Document")
    _el(doc, "name", doc_name)
    for p in pixels:
        lon = float(p["longitude"])
        lat = float(p["latitude"])
        # ~15 m half-width → degrees (local cosine for longitude).
        dlon = (LANDSAT_SCALE_M / 2.0) / (
            111320.0 * max(0.2, math.cos(math.radians(lat)))
        )
        dlat = (LANDSAT_SCALE_M / 2.0) / 110540.0
        bod = float(p["bod_mg_L"])
        cod = float(p["cod_mg_L"])
        pm = _el(doc, "Placemark")
        _el(pm, "name", f"BOD {bod:.1f} | COD {cod:.1f}")
        desc = (
            f"Stream: {p.get('stream_name', '')}\n"
            f"BOD: {bod:.2f} mg/L\n"
            f"COD: {cod:.2f} mg/L\n"
            f"Latitude: {lat:.6f}\n"
            f"Longitude: {lon:.6f}"
        )
        _el(pm, "description", desc)
        poly = _el(pm, "Polygon")
        ring = _el(_el(poly, "outerBoundaryIs"), "LinearRing")
        coords = (
            f"{lon - dlon},{lat - dlat},0 "
            f"{lon + dlon},{lat - dlat},0 "
            f"{lon + dlon},{lat + dlat},0 "
            f"{lon - dlon},{lat + dlat},0 "
            f"{lon - dlon},{lat - dlat},0"
        )
        _el(ring, "coordinates", coords)

    xml_bytes = ET.tostring(root, encoding="utf-8")
    return minidom.parseString(xml_bytes).toprettyxml(indent="  ", encoding="utf-8")


def _safe_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return f


def analyze_main_bod_cod(
    kml_bytes: bytes,
    *,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Run main-stream BOD/COD analysis.

    Daily values = one BOD/COD sample per Landsat acquisition day at each
    10 m chainage point. COD uses period Sentinel-2 TSS/NDCI correction.
    """
    features = parse_stream_features(kml_bytes)
    if start_date is None or end_date is None:
        start_date, end_date = recent_window()

    all_chainage: List[Dict[str, Any]] = []
    for f in features:
        all_chainage.extend(
            _chainage_points_for_line(
                f["geometry"],
                stream_id=f["stream_id"],
                stream_name=f["stream_name"],
                step_m=CHAINAGE_STEP_M,
            )
        )
    if not all_chainage:
        raise ValueError("Could not build 10 m chainage points from stream KML.")

    merged = unary_union([f["geometry"] for f in features])
    aoi = _geom_to_ee(merged).buffer(STREAM_BUFFER_M)
    stream_buf = _ee_stream_buffers(features)

    ls_raw = _get_landsat(aoi, start_date, end_date)
    ls_count = int(ls_raw.size().getInfo() or 0)
    if ls_count == 0:
        raise ValueError(
            f"No Landsat 8/9 TOA scenes for {start_date} → {end_date} "
            f"with CLOUD_COVER < {MAX_CLOUD}."
        )
    landsat_dates = _dates_of(ls_raw)

    ls_proj = ee.Image(ls_raw.first()).select("B3").projection()
    ls_median = (
        ls_raw.map(_mask_landsat)
        .median()
        .setDefaultProjection(ls_proj)
        .clip(aoi)
    )
    water = _water_mask(ls_median, aoi, start_date, end_date)
    tss_norm, ndci_norm, s2_count = _s2_correction(
        aoi, start_date, end_date, ls_proj
    )

    # Period-median BOD/COD (for per-stream stats + pixel KML).
    bod_med = _calibrated_bod(ls_median, water)
    cod_med = _cod_from_bod(bod_med, tss_norm, ndci_norm, water)
    bodcod_med = bod_med.addBands(cod_med)

    reducer = (
        ee.Reducer.mean()
        .combine(reducer2=ee.Reducer.min(), sharedInputs=True)
        .combine(reducer2=ee.Reducer.max(), sharedInputs=True)
        .combine(reducer2=ee.Reducer.count(), sharedInputs=True)
    )
    per_stream = bodcod_med.reduceRegions(
        collection=stream_buf,
        reducer=reducer,
        scale=LANDSAT_SCALE_M,
        crs=ls_proj,
        tileScale=4,
    )
    per_stream_info = _fc_features(per_stream)

    # Pixel samples for KML — sample each stream buffer separately (≤4k total).
    kml_pixels: List[Dict[str, Any]] = []
    for f in features:
        if len(kml_pixels) >= PIXEL_KML_MAX:
            break
        remaining = PIXEL_KML_MAX - len(kml_pixels)
        region = _geom_to_ee(f["geometry"]).buffer(STREAM_BUFFER_M)
        pixel_fc = (
            bodcod_med.addBands(ee.Image.pixelLonLat())
            .sample(
                region=region,
                projection=ls_proj,
                scale=LANDSAT_SCALE_M,
                numPixels=int(1e7),
                dropNulls=True,
                geometries=False,
                tileScale=4,
            )
            .limit(remaining)
        )
        for feat in _fc_features(pixel_fc, max_features=remaining):
            props = feat.get("properties") or {}
            bod = _safe_float(props.get("BOD_mg_L"))
            cod = _safe_float(props.get("COD_mg_L"))
            lon = _safe_float(props.get("longitude"))
            lat = _safe_float(props.get("latitude"))
            if None in (bod, cod, lon, lat):
                continue
            kml_pixels.append(
                {
                    "stream_name": f["stream_name"],
                    "longitude": lon,
                    "latitude": lat,
                    "bod_mg_L": round(bod, 3),
                    "cod_mg_L": round(cod, 3),
                }
            )

    # Daily BOD/COD at 10 m chainage — day × point-chunk to stay under EE 5k limit.
    daily_by_point: Dict[Tuple[str, float], List[Dict[str, Any]]] = {}
    point_chunks = _chunks(all_chainage, CHAINAGE_SAMPLE_CHUNK)

    for day in landsat_dates:
        start = ee.Date.parse("YYYY-MM-dd", day)
        day_col = ls_raw.filterDate(start, start.advance(1, "day")).map(
            _mask_landsat
        )
        day_img = (
            day_col.median().setDefaultProjection(ls_proj).clip(aoi)
        )
        bod = _calibrated_bod(day_img, water)
        cod = _cod_from_bod(bod, tss_norm, ndci_norm, water)
        stacked = bod.addBands(cod)

        for pts in point_chunks:
            sampled = stacked.sampleRegions(
                collection=_ee_chainage_fc(pts),
                scale=LANDSAT_SCALE_M,
                projection=ls_proj,
                geometries=False,
                tileScale=4,
            )
            for feat in _fc_features(sampled):
                props = feat.get("properties") or {}
                sid = str(props.get("stream_id") or "")
                ch = _safe_float(props.get("chainage_m"))
                bod_v = _safe_float(props.get("BOD_mg_L"))
                cod_v = _safe_float(props.get("COD_mg_L"))
                if ch is None or bod_v is None or cod_v is None:
                    continue
                key = (sid, round(ch, 1))
                daily_by_point.setdefault(key, []).append(
                    {
                        "date": str(day),
                        "bod_mg_L": round(bod_v, 3),
                        "cod_mg_L": round(cod_v, 3),
                        "bod_unit": "mg/L",
                        "cod_unit": "mg/L",
                    }
                )

    streams_out: List[Dict[str, Any]] = []
    stats_by_name = {
        (f.get("properties") or {}).get("name"): (f.get("properties") or {})
        for f in per_stream_info
    }

    for f in features:
        pts = [
            p
            for p in all_chainage
            if p["stream_id"] == f["stream_id"]
        ]
        chainages = []
        for p in pts:
            key = (p["stream_id"], round(p["chainage_m"], 1))
            series = sorted(
                daily_by_point.get(key, []),
                key=lambda r: r["date"],
            )
            # Daily entries also carry lat/lon so clients can map without join.
            daily_with_xy = [
                {
                    **row,
                    "latitude": p["latitude"],
                    "longitude": p["longitude"],
                }
                for row in series
            ]
            chainages.append(
                {
                    "chainage_m": p["chainage_m"],
                    "latitude": p["latitude"],
                    "longitude": p["longitude"],
                    "daily": daily_with_xy,
                }
            )
        st = stats_by_name.get(f["stream_name"], {})
        streams_out.append(
            {
                "stream_id": f["stream_id"],
                "stream_name": f["stream_name"],
                "chainage_label": f.get("chainage_label"),
                "stats": {
                    "bod_mg_L_mean": _safe_float(st.get("BOD_mg_L_mean")),
                    "bod_mg_L_min": _safe_float(st.get("BOD_mg_L_min")),
                    "bod_mg_L_max": _safe_float(st.get("BOD_mg_L_max")),
                    "cod_mg_L_mean": _safe_float(st.get("COD_mg_L_mean")),
                    "cod_mg_L_min": _safe_float(st.get("COD_mg_L_min")),
                    "cod_mg_L_max": _safe_float(st.get("COD_mg_L_max")),
                    "pixel_count": int(st.get("BOD_mg_L_count") or 0),
                },
                "chainage_count": len(chainages),
                "chainages": chainages,
            }
        )

    kml_bytes_out = _build_pixel_kml(kml_pixels)
    today = date.today().isoformat()

    return {
        "generated": today,
        "data_refreshed_at": datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        "method": (
            "Landsat 8+9 TOA Wang BOD with local calibration; "
            "COD = BOD×ratio + Sentinel-2 TSS/NDCI correction"
        ),
        "resolution": "daily",
        "chainage_step_m": int(CHAINAGE_STEP_M),
        "stream_buffer_m": STREAM_BUFFER_M,
        "start_date": start_date,
        "end_date": end_date,
        "landsat_image_count": ls_count,
        "landsat_dates": landsat_dates,
        "sentinel2_image_count": s2_count,
        "use_water_mask": USE_WATER_MASK,
        "use_dynamic_world": USE_DYNAMIC_WORLD,
        "calibration": {
            "bod_slope": BOD_SLOPE,
            "bod_intercept": BOD_INTERCEPT,
            "cod_bod_ratio": COD_BOD_RATIO,
            "cod_tss_w": COD_TSS_W,
            "cod_chl_w": COD_CHL_W,
            "cod_slope": COD_SLOPE,
            "cod_intercept": COD_INTERCEPT,
        },
        "stream_count": len(streams_out),
        "chainage_count": len(all_chainage),
        "streams": streams_out,
        # Period-median water pixels (30 m) with lat/lon for mapping.
        "pixel_count": len(kml_pixels),
        "pixels": [
            {
                "latitude": p["latitude"],
                "longitude": p["longitude"],
                "bod_mg_L": p["bod_mg_L"],
                "cod_mg_L": p["cod_mg_L"],
                "stream_name": p.get("stream_name"),
            }
            for p in kml_pixels
        ],
        "kml_bytes": kml_bytes_out,
        "kml_filename": "main_bod_cod_pixels.kml",
    }


# Joining-streams GEE script: relaxed MNDWI (0.0), no connectedPixelCount.
JOINING_MNDWI_MIN = 0.0
# JS sets 0.5 but documents 45 m ≈ 3 Landsat pixels — use the documented width.
JOINING_STREAM_BUFFER_M = 45.0
JOINING_PIXEL_MAX = 20000


def _joining_water_mask(
    ls_image: ee.Image,
    aoi: ee.Geometry,
    start: str,
    end: str,
) -> ee.Image:
    """Water mask for narrow joining streams (Mandvi upstreams GEE script)."""
    b3 = ls_image.select("B3")
    b5 = ls_image.select("B5")
    b6 = ls_image.select("B6")
    mndwi = b3.subtract(b6).divide(b3.add(b6))
    ls_water = (
        mndwi.gt(JOINING_MNDWI_MIN)
        .And(b5.lt(0.18))
        .And(b6.lt(0.15))
        .selfMask()
    )
    dw_col = (
        ee.ImageCollection("GOOGLE/DYNAMICWORLD/V1")
        .filterBounds(aoi)
        .filterDate(start, end)
    )
    dw_water = (
        dw_col.select("water").mean().gte(DW_WATER_THRESHOLD).selfMask()
    )
    water_mask = ls_water.unmask(0).Or(dw_water.unmask(0)).selfMask()
    if USE_WATER_MASK:
        return water_mask.rename("Water").clip(aoi)
    return ee.Image(1).rename("Water").clip(aoi)


def _ee_joining_stream_buffers(
    features: List[Dict[str, Any]],
) -> ee.FeatureCollection:
    feats = []
    for f in features:
        geom = _geom_to_ee(f["geometry"]).buffer(JOINING_STREAM_BUFFER_M)
        feats.append(
            ee.Feature(
                geom,
                {
                    "stream_id": f["stream_id"],
                    "name": f["stream_name"],
                    "chainage": f.get("chainage_label") or "",
                },
            )
        )
    return ee.FeatureCollection(feats)


def analyze_streams_bod_cod(
    kml_bytes: bytes,
    *,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Joining / sub-stream BOD/COD (Mandvi upstreams GEE workflow).

    Period-median Landsat Wang BOD + S2 COD correction.
    Returns per-stream mean/min/max and every water pixel with lat/lon.
    """
    features = parse_stream_features(kml_bytes)
    if start_date is None or end_date is None:
        start_date, end_date = recent_window()

    merged = unary_union([f["geometry"] for f in features])
    aoi = _geom_to_ee(merged).buffer(JOINING_STREAM_BUFFER_M)
    stream_buf = _ee_joining_stream_buffers(features)

    ls_raw = _get_landsat(aoi, start_date, end_date)
    ls_count = int(ls_raw.size().getInfo() or 0)
    if ls_count == 0:
        raise ValueError(
            f"No Landsat 8/9 TOA scenes for {start_date} → {end_date} "
            f"with CLOUD_COVER < {MAX_CLOUD}."
        )
    landsat_dates = _dates_of(ls_raw)

    ls_proj = ee.Image(ls_raw.first()).select("B3").projection()
    ls_median = (
        ls_raw.map(_mask_landsat)
        .median()
        .setDefaultProjection(ls_proj)
        .clip(aoi)
    )
    water = _joining_water_mask(ls_median, aoi, start_date, end_date)
    tss_norm, ndci_norm, s2_count = _s2_correction(
        aoi, start_date, end_date, ls_proj
    )

    bod = _calibrated_bod(ls_median, water)
    cod = _cod_from_bod(bod, tss_norm, ndci_norm, water)
    bodcod = bod.addBands(cod)

    reducer = (
        ee.Reducer.mean()
        .combine(reducer2=ee.Reducer.min(), sharedInputs=True)
        .combine(reducer2=ee.Reducer.max(), sharedInputs=True)
        .combine(reducer2=ee.Reducer.count(), sharedInputs=True)
    )
    per_stream = bodcod.reduceRegions(
        collection=stream_buf,
        reducer=reducer,
        scale=LANDSAT_SCALE_M,
        crs=ls_proj,
        tileScale=4,
    )
    per_stream_info = _fc_features(per_stream)
    stats_by_name = {
        (f.get("properties") or {}).get("name"): (f.get("properties") or {})
        for f in per_stream_info
    }

    # Pixel table per stream (Stream, Chainage, Lat, Lon, BOD, COD) — GEE §10.
    all_pixels: List[Dict[str, Any]] = []
    pixels_by_stream: Dict[str, List[Dict[str, Any]]] = {
        f["stream_id"]: [] for f in features
    }

    for f in features:
        if len(all_pixels) >= JOINING_PIXEL_MAX:
            break
        remaining = JOINING_PIXEL_MAX - len(all_pixels)
        region = _geom_to_ee(f["geometry"]).buffer(JOINING_STREAM_BUFFER_M)
        pixel_fc = (
            bodcod.addBands(ee.Image.pixelLonLat())
            .sample(
                region=region,
                projection=ls_proj,
                scale=LANDSAT_SCALE_M,
                numPixels=int(1e7),
                dropNulls=True,
                geometries=False,
                tileScale=4,
            )
            .limit(remaining)
        )
        for feat in _fc_features(pixel_fc, max_features=remaining):
            props = feat.get("properties") or {}
            bod_v = _safe_float(props.get("BOD_mg_L"))
            cod_v = _safe_float(props.get("COD_mg_L"))
            lon = _safe_float(props.get("longitude"))
            lat = _safe_float(props.get("latitude"))
            if None in (bod_v, cod_v, lon, lat):
                continue
            row = {
                "stream_id": f["stream_id"],
                "stream_name": f["stream_name"],
                "chainage": f.get("chainage_label") or "",
                "latitude": round(lat, 6),
                "longitude": round(lon, 6),
                "bod_mg_L": round(bod_v, 3),
                "bod_unit": "mg/L",
                "cod_mg_L": round(cod_v, 3),
                "cod_unit": "mg/L",
            }
            all_pixels.append(row)
            pixels_by_stream[f["stream_id"]].append(row)

    streams_out: List[Dict[str, Any]] = []
    for f in features:
        st = stats_by_name.get(f["stream_name"], {})
        stream_pixels = pixels_by_stream.get(f["stream_id"], [])
        streams_out.append(
            {
                "stream_id": f["stream_id"],
                "stream_name": f["stream_name"],
                "chainage_label": f.get("chainage_label"),
                "stats": {
                    "bod_mg_L_mean": _safe_float(st.get("BOD_mg_L_mean")),
                    "bod_mg_L_min": _safe_float(st.get("BOD_mg_L_min")),
                    "bod_mg_L_max": _safe_float(st.get("BOD_mg_L_max")),
                    "cod_mg_L_mean": _safe_float(st.get("COD_mg_L_mean")),
                    "cod_mg_L_min": _safe_float(st.get("COD_mg_L_min")),
                    "cod_mg_L_max": _safe_float(st.get("COD_mg_L_max")),
                    "pixel_count": int(st.get("BOD_mg_L_count") or 0),
                },
                "pixels": stream_pixels,
            }
        )

    kml_bytes_out = _build_pixel_kml(
        [
            {
                "stream_name": p["stream_name"],
                "longitude": p["longitude"],
                "latitude": p["latitude"],
                "bod_mg_L": p["bod_mg_L"],
                "cod_mg_L": p["cod_mg_L"],
            }
            for p in all_pixels[:PIXEL_KML_MAX]
        ],
        doc_name="Joining streams BOD/COD pixels",
    )
    today = date.today().isoformat()

    return {
        "generated": today,
        "data_refreshed_at": datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        "method": (
            "Joining streams: Landsat 8+9 TOA Wang BOD with local calibration; "
            "COD = BOD×ratio + Sentinel-2 TSS/NDCI correction"
        ),
        "stream_buffer_m": JOINING_STREAM_BUFFER_M,
        "start_date": start_date,
        "end_date": end_date,
        "landsat_image_count": ls_count,
        "landsat_dates": landsat_dates,
        "sentinel2_image_count": s2_count,
        "use_water_mask": USE_WATER_MASK,
        "mndwi_min": JOINING_MNDWI_MIN,
        "calibration": {
            "bod_slope": BOD_SLOPE,
            "bod_intercept": BOD_INTERCEPT,
            "cod_bod_ratio": COD_BOD_RATIO,
            "cod_tss_w": COD_TSS_W,
            "cod_chl_w": COD_CHL_W,
            "cod_slope": COD_SLOPE,
            "cod_intercept": COD_INTERCEPT,
        },
        "stream_count": len(streams_out),
        "streams": streams_out,
        "pixel_count": len(all_pixels),
        "pixels": all_pixels,
        "kml_bytes": kml_bytes_out,
        "kml_filename": "streams_bod_cod_pixels.kml",
    }
