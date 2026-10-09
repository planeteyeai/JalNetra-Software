"""
jalnetra.bank_erosion_service — Bank erosion hotspots 2016–2026 via Landsat 8/9.

Python port of the Mula-Mutha bank erosion GEE workflow (MNDWI water change).
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
START_YEAR = 2016
END_YEAR = 2026
SCALE = 30
DISPLAY_SCALE_M = 5  # fine export grid — reduces visible 30 m blocks when zoomed
SMOOTH_RADIUS_M = 90  # ~3 Landsat pixels — smooth class edges
CORRIDOR_SMOOTH_M = 60  # morphological close on water ribbon
MAX_EXPORT_PIXELS = 12_000_000
MNDWI_THRESHOLD = 0.05

HOTSPOT_CLASSES: List[Dict[str, Any]] = [
    {
        "id": 0,
        "name": "No / Very Low Erosion",
        "erosion_years_range": "0",
        "color": "#90EE90",  # light green — water corridor only
    },
    {
        "id": 1,
        "name": "Low Erosion",
        "erosion_years_range": "1-2",
        "color": "#FFFF00",
    },
    {
        "id": 2,
        "name": "Moderate Erosion",
        "erosion_years_range": "3-4",
        "color": "#FFA500",
    },
    {
        "id": 3,
        "name": "High Erosion",
        "erosion_years_range": "5-7",
        "color": "#FF0000",
    },
    {
        "id": 4,
        "name": "Very High Erosion",
        "erosion_years_range": "8+",
        "color": "#800000",
    },
]

# Class 0 light green + hotspot classes; surrounding land stays transparent
HOTSPOT_PALETTE = ["90EE90", "FFFF00", "FFA500", "FF0000", "800000"]
KML_OVERLAY_COLOR = "ffffffff"


def _polygon_coords_for_kml(geom) -> List[List[List[float]]]:
    out: List[List[List[float]]] = []
    if isinstance(geom, Polygon):
        out.append([[float(x), float(y)] for x, y in geom.exterior.coords])
    elif isinstance(geom, MultiPolygon):
        for poly in geom.geoms:
            out.append([[float(x), float(y)] for x, y in poly.exterior.coords])
    return out


def bank_erosion_geometry(kml_bytes: bytes) -> Tuple[ee.Geometry, Dict[str, Any]]:
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


def _year_end(year: int) -> ee.Date:
    if year == 2026:
        return ee.Date("2026-08-20")
    return ee.Date.fromYMD(year + 1, 1, 1)


def _mask_landsat(image: ee.Image) -> ee.Image:
    qa = image.select("QA_PIXEL")
    cloud_shadow = qa.bitwiseAnd(1 << 4).eq(0)
    snow = qa.bitwiseAnd(1 << 5).eq(0)
    cloud = qa.bitwiseAnd(1 << 3).eq(0)
    dilated_cloud = qa.bitwiseAnd(1 << 1).eq(0)
    mask = cloud_shadow.And(snow).And(cloud).And(dilated_cloud)

    optical = (
        image.select(["SR_B2", "SR_B3", "SR_B4", "SR_B5", "SR_B6", "SR_B7"])
        .multiply(0.0000275)
        .add(-0.2)
    )
    return optical.updateMask(mask).copyProperties(image, image.propertyNames())


def _landsat_collection(aoi: ee.Geometry) -> ee.ImageCollection:
    l8 = ee.ImageCollection("LANDSAT/LC08/C02/T1_L2")
    l9 = ee.ImageCollection("LANDSAT/LC09/C02/T1_L2")
    return l8.merge(l9).filterBounds(aoi).map(_mask_landsat)


def _get_year_image(
    landsat: ee.ImageCollection, aoi: ee.Geometry, year: int
) -> ee.Image:
    start = ee.Date.fromYMD(year, 1, 1)
    end = _year_end(year)
    collection = landsat.filterBounds(aoi).filterDate(start, end)
    return collection.median().clip(aoi)


def _get_water(
    landsat: ee.ImageCollection, aoi: ee.Geometry, year: int
) -> ee.Image:
    image = _get_year_image(landsat, aoi, year)
    mndwi = image.normalizedDifference(["SR_B3", "SR_B6"]).rename("MNDWI")
    water = mndwi.gt(MNDWI_THRESHOLD)
    connected = water.selfMask().connectedPixelCount(100, True)
    return (
        water.updateMask(connected.gte(3))
        .rename("Water")
        .selfMask()
    )


def _safe_area_m2(result: Dict[str, Any]) -> float:
    value = result.get("area", 0)
    if value is None:
        return 0.0
    return float(value)


def _reduce_area_m2(mask: ee.Image, aoi: ee.Geometry) -> float:
    result = (
        ee.Image.pixelArea()
        .updateMask(mask)
        .reduceRegion(
            reducer=ee.Reducer.sum(),
            geometry=aoi,
            scale=SCALE,
            maxPixels=1e13,
            bestEffort=True,
        )
        .getInfo()
        or {}
    )
    return _safe_area_m2(result)


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
    for candidate in (DISPLAY_SCALE_M, float(SCALE)):
        if _estimate_pixel_count(box, candidate) <= MAX_EXPORT_PIXELS:
            return candidate
    ratio = math.sqrt(_estimate_pixel_count(box, SCALE) / MAX_EXPORT_PIXELS)
    return round(SCALE * ratio, 1)


def _smooth_classified(image: ee.Image) -> ee.Image:
    """Two-pass mode filter — softer edges, no square block corners."""
    first = image.focal_mode(radius=SMOOTH_RADIUS_M, units="meters")
    return first.focal_mode(radius=SMOOTH_RADIUS_M // 2, units="meters")


def _smooth_corridor_mask(mask: ee.Image) -> ee.Image:
    """Morphological close — smooth water-ribbon outline, stay inside river."""
    closed = mask.focal_max(radius=CORRIDOR_SMOOTH_M, units="meters").focal_min(
        radius=CORRIDOR_SMOOTH_M, units="meters"
    )
    return closed


def _aoi_mask_image(aoi: ee.Geometry) -> ee.Image:
    return ee.Image.constant(1).clip(aoi).selfMask()


def _pct(part: float, whole: float) -> float:
    if whole <= 0:
        return 0.0
    return round(part / whole * 100.0, 1)


def _hotspot_vis_image(
    hotspot_class: ee.Image,
    aoi: ee.Geometry,
    water_corridor: ee.Image,
) -> ee.Image:
    """
    Class-masked overlay: light-green No Erosion on water corridor only,
    hotspot colours on erosion pixels. Strict AOI clip — nothing outside KML.
    """
    export_scale = _export_scale_for_geometry(aoi)
    aoi_mask = _aoi_mask_image(aoi)
    clipped = hotspot_class.clip(aoi)

    # River ribbon + erosion bank pixels; smoothed so edges are not staircase blocks
    corridor_raw = water_corridor.Or(clipped.gt(0)).clip(aoi)
    corridor = _smooth_corridor_mask(corridor_raw)

    smoothed = _smooth_classified(clipped)
    # Re-mask after smoothing so classes cannot spill onto surrounding land
    masked = smoothed.updateMask(corridor).updateMask(aoi_mask)

    scaled = masked.reproject(crs="EPSG:4326", scale=export_scale)
    return scaled.visualize(min=0, max=4, palette=HOTSPOT_PALETTE).updateMask(
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
    """GroundOverlay only — no placemark borders (matches screenshot)."""
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


def analyze_bank_erosion(
    aoi: ee.Geometry,
    *,
    aoi_info: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Bank erosion hotspots for fixed period 2016–2026 (no buffer).

    Hotspot class = count of year-to-year periods where land became water.
    """
    landsat = _landsat_collection(aoi)
    analysis_area_ha = round(float(aoi.area(1).divide(10000).getInfo()), 2)

    # Pre-load yearly water masks (unmasked 0/1 for change logic)
    water_by_year: Dict[int, ee.Image] = {}
    image_counts: Dict[int, int] = {}
    for year in range(START_YEAR, END_YEAR + 1):
        start = ee.Date.fromYMD(year, 1, 1)
        end = _year_end(year)
        count = int(
            landsat.filterBounds(aoi).filterDate(start, end).size().getInfo() or 0
        )
        image_counts[year] = count
        if count == 0:
            raise ValueError(
                f"No Landsat 8/9 scenes found for {year} over the KML area."
            )
        water_by_year[year] = _get_water(landsat, aoi, year).unmask(0)

    hotspot = ee.Image.constant(0).rename("Erosion_Years").clip(aoi)
    period_stats: List[Dict[str, Any]] = []

    for year in range(START_YEAR + 1, END_YEAR + 1):
        old_water = water_by_year[year - 1]
        new_water = water_by_year[year]

        erosion = new_water.eq(1).And(old_water.eq(0))
        accretion = old_water.eq(1).And(new_water.eq(0))

        erosion_m2 = _reduce_area_m2(erosion, aoi)
        accretion_m2 = _reduce_area_m2(accretion, aoi)
        old_water_m2 = _reduce_area_m2(old_water, aoi)
        new_water_m2 = _reduce_area_m2(new_water, aoi)

        erosion_ha = round(erosion_m2 / 10000.0, 4)
        accretion_ha = round(accretion_m2 / 10000.0, 4)
        old_water_ha = round(old_water_m2 / 10000.0, 4)
        new_water_ha = round(new_water_m2 / 10000.0, 4)
        net_change_ha = round(accretion_ha - erosion_ha, 4)
        total_bank_change_ha = round(erosion_ha + accretion_ha, 4)

        old_den = max(old_water_m2, 1.0)
        erosion_pct = round(erosion_m2 / old_den * 100.0, 2)
        accretion_pct = round(accretion_m2 / old_den * 100.0, 2)
        ratio = round(erosion_ha / max(accretion_ha, 0.0001), 4)

        hotspot = hotspot.add(erosion)

        period_stats.append(
            {
                "from_year": year - 1,
                "to_year": year,
                "period": f"{year - 1}-{year}",
                "analysis_period": (
                    "Jan-2026 to Aug-2026" if year == 2026 else "Full Year"
                ),
                "satellite": "Landsat 8/9",
                "resolution_m": SCALE,
                "mndwi_threshold": MNDWI_THRESHOLD,
                "image_count": image_counts[year],
                "old_water_ha": old_water_ha,
                "new_water_ha": new_water_ha,
                "erosion_ha": erosion_ha,
                "erosion_km2": round(erosion_m2 / 1_000_000.0, 6),
                "erosion_percent": erosion_pct,
                "accretion_ha": accretion_ha,
                "accretion_km2": round(accretion_m2 / 1_000_000.0, 6),
                "accretion_percent": accretion_pct,
                "net_change_ha": net_change_ha,
                "total_bank_change_ha": total_bank_change_ha,
                "erosion_to_accretion": ratio,
            }
        )

    hotspot_class = (
        hotspot.where(hotspot.eq(0), 0)
        .where(hotspot.gte(1).And(hotspot.lte(2)), 1)
        .where(hotspot.gte(3).And(hotspot.lte(4)), 2)
        .where(hotspot.gte(5).And(hotspot.lte(7)), 3)
        .where(hotspot.gte(8), 4)
        .rename("Erosion_Hotspot")
        .clip(aoi)
    )

    # Union of all yearly water — No Erosion green stays on river only, not land
    ever_water = ee.Image(0)
    for year in range(START_YEAR, END_YEAR + 1):
        ever_water = ever_water.Or(water_by_year[year].eq(1))
    ever_water = ever_water.clip(aoi)

    categories = []
    for cls in HOTSPOT_CLASSES:
        cid = int(cls["id"])
        if cid == 0:
            # No Erosion area = class 0 inside water corridor only
            class_mask = hotspot_class.eq(0).And(ever_water)
        else:
            class_mask = hotspot_class.eq(cid)
        ha = round(_reduce_area_m2(class_mask, aoi) / 10000.0, 2)
        categories.append(
            {
                "id": cid,
                "name": cls["name"],
                "erosion_years_range": cls["erosion_years_range"],
                "color": cls["color"],
                "area_ha": ha,
                "percent": _pct(ha, analysis_area_ha),
            }
        )

    vis = _hotspot_vis_image(hotspot_class, aoi, ever_water)
    export_scale_m = _export_scale_for_geometry(aoi)
    png = _export_overlay_png(vis, aoi)
    box = _aoi_box(aoi)
    legend = [
        f"{c['color']}  {c['id']} = {c['name']} ({c['erosion_years_range']})"
        for c in HOTSPOT_CLASSES
    ]

    kml_bytes = _build_kml(
        title="Bank Erosion Hotspots 2016-2026",
        description=(
            "Bank erosion hotspots · Landsat 8/9 · MNDWI\n"
            f"Period: {START_YEAR}–{END_YEAR} (2026 through Aug 20)\n"
            f"Analysis area: {analysis_area_ha} ha\n"
            "Light-green = No Erosion (water corridor only)\n"
            "Hotspot = number of year-to-year periods with detected erosion"
        ),
        box=box,
        overlay_name="Bank Erosion Hotspots",
        png_bytes=png,
        legend_lines=["BANK EROSION HOTSPOT", *legend],
    )

    result: Dict[str, Any] = {
        "start_year": START_YEAR,
        "end_year": END_YEAR,
        "satellite": "Landsat 8/9",
        "resolution_m": SCALE,
        "mndwi_threshold": MNDWI_THRESHOLD,
        "analysis_area_ha": analysis_area_ha,
        "yearly_image_counts": [
            {"year": y, "image_count": image_counts[y]}
            for y in range(START_YEAR, END_YEAR + 1)
        ],
        "percent_basis": "analysis_area",
        "categories": categories,
        "legend": HOTSPOT_CLASSES,
        "period_statistics": period_stats,
        "export_scale_m": export_scale_m,
        "kml_bytes": kml_bytes,
        "notes": {
            "erosion": "Land to Water",
            "accretion": "Water to Land",
            "hotspot": (
                "Number of year-to-year periods with detected erosion"
            ),
            "no_erosion_fill": (
                "Light green only on water corridor — does not spill onto surrounding land"
            ),
            "year_2026": "January–August 19 (incomplete year)",
        },
    }
    if aoi_info:
        result["aoi"] = aoi_info
    return result
