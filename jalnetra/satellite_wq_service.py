"""
jalnetra.satellite_wq_service — Satellite-only water-quality layers.

Python port of the simple GEE JS workflow (Johansen 2024 chlorophyll /
Chowdhury 2025 E. coli features):

  1. Eutrophication — Carlson TSI trophic class from chlorophyll-a
  2. Nitrogen       — high-nitrogen risk class (not mg N/L)
  3. Coliform       — E. coli bathing class (relative rank, no field CFU)

Uses the nearest clear Sentinel-2 date (tile cloud < 20%).
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
ANALYSIS_SCALE = 20
DISPLAY_SCALE_M = 5
MAX_EXPORT_PIXELS = 12_000_000
# Match original JS Filter.lt(..., cloud) with threshold 20.
MAX_CLOUD_S2 = 20
JRC_OCCURRENCE_MIN = 20
WATER_FOCAL_M = 20
ECOLI_FOCAL_M = 45
EPS = 0.0005

# Palettes from the GEE JS legend (hex without # for EE visualize).
PAL_TROPHIC = ["2c7bb6", "abd9e9", "fdae61", "d7191c"]
PAL_N = ["1a9641", "ffffbf", "d7191c"]
PAL_ECOLI = ["2c7bb6", "91cf60", "fdae61", "d7191c"]

TROPHIC_CLASSES: List[Dict[str, Any]] = [
    {
        "id": 1,
        "name": "Oligotrophic",
        "range": "< 2.6 ug/L (TSI < 40)",
        "color": "#2c7bb6",
    },
    {
        "id": 2,
        "name": "Mesotrophic",
        "range": "2.6 - 7.3 ug/L (TSI 40-50)",
        "color": "#abd9e9",
    },
    {
        "id": 3,
        "name": "Eutrophic",
        "range": "7.3 - 56 ug/L (TSI 50-70)",
        "color": "#fdae61",
    },
    {
        "id": 4,
        "name": "Hypereutrophic",
        "range": "> 56 ug/L (TSI > 70)",
        "color": "#d7191c",
    },
]

NITROGEN_CLASSES: List[Dict[str, Any]] = [
    {"id": 1, "name": "Low", "range": "risk <= 0.33", "color": "#1a9641"},
    {"id": 2, "name": "Moderate", "range": "0.33 - 0.66", "color": "#ffffbf"},
    {"id": 3, "name": "High", "range": "risk > 0.66", "color": "#d7191c"},
]

COLIFORM_CLASSES: List[Dict[str, Any]] = [
    {
        "id": 1,
        "name": "Good",
        "range": "<= 250 CFU/100 ml (relative)",
        "color": "#2c7bb6",
    },
    {
        "id": 2,
        "name": "Moderate",
        "range": "260 - 500 (relative)",
        "color": "#91cf60",
    },
    {
        "id": 3,
        "name": "High",
        "range": "500 - 2000 (relative)",
        "color": "#fdae61",
    },
    {
        "id": 4,
        "name": "Highly risk",
        "range": "> 2000 (relative)",
        "color": "#d7191c",
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


def satellite_wq_geometry(kml_bytes: bytes) -> Tuple[ee.Geometry, Dict[str, Any]]:
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


def _class_areas_ha(
    class_image: ee.Image, geometry: ee.Geometry, scale: int = ANALYSIS_SCALE
) -> Dict[int, float]:
    # Fixed CRS/scale collapses expensive nested projections before reduceRegion.
    classified = class_image.reproject(crs="EPSG:4326", scale=scale).rename("class")
    area_img = ee.Image.pixelArea().divide(10000).addBands(classified)
    result = area_img.reduceRegion(
        reducer=ee.Reducer.sum().group(groupField=1, groupName="class"),
        geometry=geometry,
        scale=scale,
        maxPixels=1e13,
        bestEffort=True,
        tileScale=8,
    ).getInfo() or {}
    areas: Dict[int, float] = {}
    for group in result.get("groups", []) or []:
        cid = int(group["class"])
        areas[cid] = round(float(group["sum"]), 2)
    return areas


def _prep_s2(img: ee.Image, jrc: ee.Image) -> ee.Image:
    """Prep one Sentinel-2 scene — same math as the GEE JS `prep`."""
    water = (
        img.select("SCL")
        .eq(6)
        .And(jrc.gte(JRC_OCCURRENCE_MIN))
        .focal_min(radius=WATER_FOCAL_M, kernelType="square", units="meters")
    )
    sr = (
        img.select(["B1", "B4", "B5", "B11", "B12"])
        .divide(10000)
        .updateMask(water)
    )

    # Chlorophyll-a (ug/L): NDCI + 2BDA regressions (Johansen 2024), averaged
    ndci = sr.select("B5").subtract(sr.select("B4")).divide(
        sr.select("B5").add(sr.select("B4"))
    )
    bda2 = sr.select("B5").divide(sr.select("B4"))
    chl = (
        ndci.multiply(203.97)
        .add(21.08)
        .add(bda2.multiply(59.40).subtract(36.31))
        .divide(2)
        .clamp(0, 200)
        .rename("CHL")
    )

    # E. coli spectral features (Chowdhury 2025), smoothed ~90 m
    sm = (
        sr.select(["B1", "B11", "B12"])
        .focal_mean(radius=ECOLI_FOCAL_M, kernelType="square", units="meters")
        .updateMask(water)
    )
    b1 = sm.select("B1")
    b11 = sm.select("B11")
    b12 = sm.select("B12")
    f1 = b11.max(EPS).divide(b12.max(EPS)).rename("F1")
    f2 = b11.max(EPS).divide(b1.max(EPS)).rename("F2")
    f3 = b11.subtract(b1).divide(b11.add(b1)).rename("F3")

    return ee.Image.cat([chl, f1, f2, f3]).copyProperties(
        img, ["system:time_start"]
    )


def _land_frac(wc: ee.Image, cls: int, radius: int, aoi: ee.Geometry) -> ee.Image:
    """
    Fraction of WorldCover class within `radius` metres.

    Matches the GEE JS landFrac intent (100 m grid → focalMean over radius).
    Clipped to AOI+buffer; uses a light 100 m reproject instead of
    reduceResolution (which was timing out / returning HTTP 400 on export).
    """
    region = aoi.buffer(radius + 200, 1)
    return (
        wc.eq(cls)
        .clip(region)
        .reproject(crs="EPSG:4326", scale=100)
        .focal_mean(radius=radius, units="meters")
        .clip(aoi)
    )


def _scale01_client(
    feats: ee.Image, band: str, pct: Dict[str, Any]
) -> ee.Image:
    """Rescale band 0–1 from AOI 5th/95th percentiles (client constants)."""
    lo = float(pct.get(f"{band}_p5") or 0.0)
    hi = float(pct.get(f"{band}_p95") or 0.0)
    span = max(hi - lo, 1e-6)
    return feats.select(band).subtract(lo).divide(span).clamp(0, 1)


def _build_s2_core(
    aoi: ee.Geometry, start_date: str, end_date: str
) -> Dict[str, Any]:
    """Sentinel-2 prep + chlorophyll / TSI / trophic class for the clear date."""
    jrc = ee.Image("JRC/GSW1_4/GlobalSurfaceWater").select("occurrence").unmask(0)

    col = (
        ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
        .filterBounds(aoi)
        .filterDate(start_date, end_date)
        .filter(ee.Filter.lt("CLOUDY_PIXEL_PERCENTAGE", MAX_CLOUD_S2))
        .map(lambda img: _prep_s2(img, jrc))
    )
    s2_count = int(col.size().getInfo() or 0)
    if s2_count == 0:
        raise ValueError(
            f"No Sentinel-2 scenes with < {MAX_CLOUD_S2}% cloud found between "
            f"{start_date} and {end_date} over the KML area."
        )

    med = col.median().clip(aoi)
    enough = med.select("CHL").mask()
    chl = med.select("CHL").updateMask(enough)
    tsi = chl.max(0.1).log().multiply(9.81).add(30.6)
    trophic = (
        tsi.gt(40)
        .add(tsi.gt(50))
        .add(tsi.gt(70))
        .add(1)
        .rename("TROPHIC_CLASS")
        .updateMask(enough)
        .clip(aoi)
    )

    first = ee.Image(col.sort("system:time_start", False).first())
    s2_date = (
        ee.Date(first.get("system:time_start")).format("YYYY-MM-dd HH:mm").getInfo()
    )
    s2_cloud = first.get("CLOUDY_PIXEL_PERCENTAGE").getInfo()

    return {
        "med": med,
        "enough": enough,
        "chl": chl,
        "tsi": tsi,
        "trophic": trophic,
        "s2_count": s2_count,
        "s2_date": s2_date,
        "s2_cloud": s2_cloud,
    }


def _build_nitrogen_layer(
    core: Dict[str, Any],
    aoi: ee.Geometry,
    *,
    no2_start: str,
    no2_end: str,
) -> Dict[str, Any]:
    """High-nitrogen risk class on top of S2 core (WorldCover + S5P NO2)."""
    tsi = core["tsi"]
    enough = core["enough"]
    wc = ee.ImageCollection("ESA/WorldCover/v200").first().select("Map")
    crop = _land_frac(wc, 40, 3000, aoi)
    built = _land_frac(wc, 50, 2000, aoi)
    no2 = (
        ee.ImageCollection("COPERNICUS/S5P/OFFL/L3_NO2")
        .filterBounds(aoi)
        .filterDate(no2_start, no2_end)
        .select("tropospheric_NO2_column_number_density")
        .mean()
        .clip(aoi)
    )

    chl_part = tsi.subtract(40).divide(30).clamp(0, 1)
    land_part = (
        crop.unmask(0).add(built.unmask(0).multiply(0.5)).divide(0.5).clamp(0, 1)
    )
    no2_part = no2.unmask(0).divide(1.5e-4).clamp(0, 1)
    n_risk = (
        chl_part.multiply(0.45)
        .add(land_part.multiply(0.40))
        .add(no2_part.multiply(0.15))
        .rename("N_RISK")
    )
    n_class = (
        n_risk.gt(0.33)
        .add(n_risk.gt(0.66))
        .add(1)
        .rename("N_CLASS")
        .updateMask(enough)
        .clip(aoi)
    )
    return {
        **core,
        "n_risk": n_risk.updateMask(enough).clip(aoi),
        "n_class": n_class,
        "no2_start": no2_start,
        "no2_end": no2_end,
    }


def _build_coliform_layer(core: Dict[str, Any], aoi: ee.Geometry) -> Dict[str, Any]:
    """E. coli class on top of S2 core (F1–F3 percentiles + urban pressure)."""
    med = core["med"]
    enough = core["enough"]
    feats = med.select(["F1", "F2", "F3"])
    pct = (
        feats.reduceRegion(
            reducer=ee.Reducer.percentile([5, 95]),
            geometry=aoi,
            scale=60,
            maxPixels=1e9,
            bestEffort=True,
            tileScale=4,
        ).getInfo()
        or {}
    )
    fib = (
        _scale01_client(feats, "F1", pct)
        .add(_scale01_client(feats, "F2", pct))
        .add(_scale01_client(feats, "F3", pct))
        .divide(3)
        .updateMask(enough)
    )
    wc = ee.ImageCollection("ESA/WorldCover/v200").first().select("Map")
    built = _land_frac(wc, 50, 2000, aoi)
    urban = built.unmask(0).divide(0.25).clamp(0, 1)
    ecoli_score = fib.multiply(0.75).add(urban.multiply(0.25)).rename("ECOLI_SCORE")
    ecoli_class = (
        ecoli_score.gt(0.25)
        .add(ecoli_score.gt(0.5))
        .add(ecoli_score.gt(0.75))
        .add(1)
        .rename("ECOLI_CLASS")
        .updateMask(enough)
        .clip(aoi)
    )
    return {
        **core,
        "ecoli_score": ecoli_score,
        "ecoli_class": ecoli_class,
    }


def _class_vis_image(
    class_image: ee.Image,
    aoi: ee.Geometry,
    *,
    min_val: int,
    max_val: int,
    palette: List[str],
) -> ee.Image:
    export_scale = _export_scale_for_geometry(aoi)
    # Bake scale/CRS before visualize so Drive/thumbnail requests stay light.
    clipped = (
        class_image.clip(aoi)
        .reproject(crs="EPSG:4326", scale=max(export_scale, float(ANALYSIS_SCALE)))
    )
    return clipped.visualize(min=min_val, max=max_val, palette=palette).updateMask(
        clipped.mask()
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
        # Fallback: bounded thumbnail (avoids full-region download 400s on heavy graphs)
        box = _aoi_box(geometry)
        width_m = (box["east"] - box["west"]) * 111320
        height_m = (box["north"] - box["south"]) * 110540
        max_side = max(width_m, height_m) / max(export_scale, 1.0)
        dim = int(min(max(max_side, 256), 2048))
        url = vis_image.getThumbURL(
            {"region": geometry, "dimensions": dim, "format": "png"}
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


def _categories_from_areas(
    class_defs: List[Dict[str, Any]], areas: Dict[int, float], total_ha: float
) -> List[Dict[str, Any]]:
    categories = []
    for cls in class_defs:
        cid = int(cls["id"])
        ha = areas.get(cid, 0.0)
        categories.append(
            {
                "id": cid,
                "name": cls["name"],
                "range": cls["range"],
                "color": cls["color"],
                "area_ha": ha,
                "percent": _pct(ha, total_ha),
            }
        )
    return categories


def _layer_result(
    *,
    layer_key: str,
    title: str,
    class_image: ee.Image,
    class_defs: List[Dict[str, Any]],
    palette: List[str],
    min_val: int,
    max_val: int,
    aoi: ee.Geometry,
    start_date: str,
    end_date: str,
    s2_date: str,
    s2_count: int,
    s2_cloud: Any,
    description_extra: str,
    aoi_info: Optional[Dict[str, Any]],
    disclaimer: str,
) -> Dict[str, Any]:
    areas = _class_areas_ha(class_image, aoi)
    total_ha = round(sum(areas.values()), 2)
    analysis_area_ha = round(float(aoi.area(1).divide(10000).getInfo()), 2)
    categories = _categories_from_areas(class_defs, areas, total_ha)

    vis = _class_vis_image(
        class_image, aoi, min_val=min_val, max_val=max_val, palette=palette
    )
    png = _export_overlay_png(vis, aoi)
    box = _aoi_box(aoi)
    legend = [f"{c['color']}  {c['name']} ({c['range']})" for c in class_defs]

    input_lines = aoi_info.get("input_line_coords") if aoi_info else None
    input_polys = aoi_info.get("input_polygon_coords") if aoi_info else None

    kml_bytes = _build_kml(
        title=title,
        description=(
            f"{title} · {start_date} to {end_date}\n"
            f"Nearest clear Sentinel-2: {s2_date} "
            f"(cloud < {MAX_CLOUD_S2}%, tile cloud={s2_cloud})\n"
            f"Analysis area: {analysis_area_ha} ha · Classified water: {total_ha} ha\n"
            f"{description_extra}"
        ),
        box=box,
        overlay_name=title,
        png_bytes=png,
        legend_lines=[title.upper(), *legend],
        input_line_coords=input_lines,
        input_polygon_coords=input_polys,
    )

    result: Dict[str, Any] = {
        "layer": layer_key,
        "title": title,
        "start_date": start_date,
        "end_date": end_date,
        "sentinel2_image_count": s2_count,
        "latest_sentinel2_date": s2_date,
        "latest_sentinel2_cloud_percentage": s2_cloud,
        "max_cloud_percent": MAX_CLOUD_S2,
        "analysis_area_ha": analysis_area_ha,
        "classified_water_area_ha": total_ha,
        "percent_basis": "classified_water_area",
        "categories": categories,
        "legend": class_defs,
        "disclaimer": disclaimer,
        "export_scale_m": _export_scale_for_geometry(aoi),
        "resolution_m": ANALYSIS_SCALE,
        "kml_bytes": kml_bytes,
    }
    if aoi_info:
        result["aoi"] = aoi_info
    return result


def analyze_eutrophication(
    aoi_geometry: ee.Geometry,
    start_date: str,
    end_date: str,
    *,
    aoi_info: Optional[Dict[str, Any]] = None,
    no2_window: Optional[Tuple[str, str]] = None,
) -> Dict[str, Any]:
    """Trophic class from chlorophyll-a (Carlson TSI)."""
    del no2_window  # unused for this layer; kept for shared API runner signature
    layers = _build_s2_core(aoi_geometry, start_date, end_date)
    return _layer_result(
        layer_key="eutrophication",
        title="Eutrophication (trophic class)",
        class_image=layers["trophic"],
        class_defs=TROPHIC_CLASSES,
        palette=PAL_TROPHIC,
        min_val=1,
        max_val=4,
        aoi=aoi_geometry,
        start_date=start_date,
        end_date=end_date,
        s2_date=layers["s2_date"],
        s2_count=layers["s2_count"],
        s2_cloud=layers["s2_cloud"],
        description_extra=(
            "Chlorophyll-a ug/L via NDCI/2BDA (Johansen 2024) → Carlson TSI classes."
        ),
        aoi_info=aoi_info,
        disclaimer=(
            "Trophic class from satellite chlorophyll-a (Carlson TSI). "
            "Not a lab-measured nutrient concentration."
        ),
    )


def analyze_nitrogen(
    aoi_geometry: ee.Geometry,
    start_date: str,
    end_date: str,
    *,
    aoi_info: Optional[Dict[str, Any]] = None,
    no2_window: Optional[Tuple[str, str]] = None,
) -> Dict[str, Any]:
    """High-nitrogen risk class (chlorophyll + land sources + S5P NO2)."""
    n0, n1 = no2_window or (start_date, end_date)
    core = _build_s2_core(aoi_geometry, start_date, end_date)
    layers = _build_nitrogen_layer(
        core, aoi_geometry, no2_start=n0, no2_end=n1
    )
    result = _layer_result(
        layer_key="nitrogen",
        title="High-nitrogen risk class",
        class_image=layers["n_class"],
        class_defs=NITROGEN_CLASSES,
        palette=PAL_N,
        min_val=1,
        max_val=3,
        aoi=aoi_geometry,
        start_date=start_date,
        end_date=end_date,
        s2_date=layers["s2_date"],
        s2_count=layers["s2_count"],
        s2_cloud=layers["s2_cloud"],
        description_extra=(
            "Risk = 0.45·chl + 0.40·land (crop/built) + 0.15·NO2. "
            f"NO2 window: {layers['no2_start']} to {layers['no2_end']}."
        ),
        aoi_info=aoi_info,
        disclaimer=(
            "Nitrogen risk class only — NOT mg N/L or laboratory nitrogen."
        ),
    )
    result["no2_window"] = {
        "start": layers["no2_start"],
        "end": layers["no2_end"],
    }
    return result


def analyze_coliform(
    aoi_geometry: ee.Geometry,
    start_date: str,
    end_date: str,
    *,
    aoi_info: Optional[Dict[str, Any]] = None,
    no2_window: Optional[Tuple[str, str]] = None,
) -> Dict[str, Any]:
    """E. coli bathing class — relative 0–1 rank without field CFU data."""
    del no2_window  # unused for this layer; kept for shared API runner signature
    core = _build_s2_core(aoi_geometry, start_date, end_date)
    layers = _build_coliform_layer(core, aoi_geometry)
    return _layer_result(
        layer_key="coliform",
        title="E. coli / coliform class",
        class_image=layers["ecoli_class"],
        class_defs=COLIFORM_CLASSES,
        palette=PAL_ECOLI,
        min_val=1,
        max_val=4,
        aoi=aoi_geometry,
        start_date=start_date,
        end_date=end_date,
        s2_date=layers["s2_date"],
        s2_count=layers["s2_count"],
        s2_cloud=layers["s2_cloud"],
        description_extra=(
            "Relative rank of SWIR/blue FIB score (Chowdhury 2025) + urban pressure. "
            "CFU labels are paper class names — map is relative without field data."
        ),
        aoi_info=aoi_info,
        disclaimer=(
            "Coliform / E. coli class is a relative satellite rank. "
            "It is NOT laboratory CFU/100 ml."
        ),
    )
