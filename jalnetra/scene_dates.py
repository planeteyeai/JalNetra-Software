"""
jalnetra.scene_dates — automatic image-date selection (no user date input).

Sentinel-2: look back CLEAR_WINDOW_DAYS from today, keep scenes with tile
cloud <= S2_MAX_CLOUD_PCT, then walk dates newest → oldest and take the first
date whose cloud fraction *inside the AOI* is <= CLEAR_AOI_CLOUD_PCT. If no
date in the window is that clear, take the least-cloudy date in the same
window (never outside it).

Sentinel-1: SAR is cloud-independent, so the nearest date in the same window
whose scenes cover the AOI is used.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Tuple

import ee

S2_COLLECTION = "COPERNICUS/S2_SR_HARMONIZED"
S1_COLLECTION = "COPERNICUS/S1_GRD"
DYNAMIC_WORLD = "GOOGLE/DYNAMICWORLD/V1"

CLEAR_WINDOW_DAYS = 30
S2_MAX_CLOUD_PCT = 40
CLEAR_AOI_CLOUD_PCT = 5.0
MIN_AOI_COVERAGE_PCT = 80.0
STATS_SCALE_M = 60
# SCL: 3 cloud shadow, 8 cloud medium prob, 9 cloud high prob, 10 thin cirrus
_SCL_CLOUD_CLASSES = (3, 8, 9, 10)


def recent_window(
    today: Optional[date] = None, days: int = CLEAR_WINDOW_DAYS
) -> Tuple[str, str]:
    """(start, end_exclusive) ISO dates covering the last `days` days incl. today."""
    today = today or date.today()
    start = today - timedelta(days=days)
    end_exclusive = today + timedelta(days=1)
    return start.isoformat(), end_exclusive.isoformat()


def _next_day(day: str) -> str:
    return (date.fromisoformat(day) + timedelta(days=1)).isoformat()


def _daily_stats(
    collection: ee.ImageCollection,
    geometry: ee.Geometry,
    *,
    band: str,
    with_cloud: bool,
) -> List[Dict[str, Any]]:
    """One row per acquisition day: AOI coverage % and (S2) AOI cloud %."""
    days = (
        collection.aggregate_array("system:time_start")
        .map(lambda t: ee.Date(t).format("YYYY-MM-dd"))
        .distinct()
    )

    def _per_day(day):
        start = ee.Date.parse("YYYY-MM-dd", day)
        day_col = collection.filterDate(start, start.advance(1, "day"))
        mosaic = day_col.mosaic()
        layer = mosaic.select(band)
        stack = layer.mask().rename("covered")
        if with_cloud:
            cloudy = layer.eq(_SCL_CLOUD_CLASSES[0])
            for cls in _SCL_CLOUD_CLASSES[1:]:
                cloudy = cloudy.Or(layer.eq(cls))
            stack = stack.addBands(cloudy.rename("cloud"))
        stats = stack.reduceRegion(
            reducer=ee.Reducer.mean(),
            geometry=geometry,
            scale=STATS_SCALE_M,
            maxPixels=1e9,
            bestEffort=True,
            tileScale=4,
        )
        props = {
            "date": day,
            "covered": stats.get("covered"),
            "tiles": day_col.size(),
        }
        if with_cloud:
            props["cloud"] = stats.get("cloud")
            props["tile_cloud"] = day_col.aggregate_mean("CLOUDY_PIXEL_PERCENTAGE")
        return ee.Feature(None, props)

    info = ee.FeatureCollection(days.map(_per_day)).getInfo() or {}
    rows: List[Dict[str, Any]] = []
    for feat in info.get("features", []):
        p = feat.get("properties") or {}
        if not p.get("date"):
            continue
        covered = p.get("covered")
        row: Dict[str, Any] = {
            "date": p["date"],
            "aoi_coverage_percent": round(float(covered) * 100.0, 1)
            if covered is not None
            else 0.0,
            "tiles": int(p.get("tiles") or 0),
        }
        if with_cloud:
            cloud = p.get("cloud")
            tile_cloud = p.get("tile_cloud")
            row["aoi_cloud_percent"] = (
                round(float(cloud) * 100.0, 2) if cloud is not None else 100.0
            )
            row["tile_cloud_percent"] = (
                round(float(tile_cloud), 2) if tile_cloud is not None else None
            )
        rows.append(row)
    rows.sort(key=lambda r: r["date"], reverse=True)
    return rows


def select_clear_s2_date(
    geometry: ee.Geometry,
    *,
    today: Optional[date] = None,
    window_days: int = CLEAR_WINDOW_DAYS,
    max_cloud_pct: float = S2_MAX_CLOUD_PCT,
    clear_aoi_cloud_pct: float = CLEAR_AOI_CLOUD_PCT,
    require_dynamic_world: bool = False,
) -> Dict[str, Any]:
    """
    Nearest-to-today clear Sentinel-2 date inside the look-back window.

    Returns `start_date` / `end_date` (exclusive, = start + 1 day) ready to be
    passed to the existing analysis functions, plus selection metadata.
    """
    window_start, window_end = recent_window(today, window_days)
    collection = (
        ee.ImageCollection(S2_COLLECTION)
        .filterBounds(geometry)
        .filterDate(window_start, window_end)
        .filter(ee.Filter.lte("CLOUDY_PIXEL_PERCENTAGE", max_cloud_pct))
    )
    if require_dynamic_world:
        dw_ids = (
            ee.ImageCollection(DYNAMIC_WORLD)
            .filterBounds(geometry)
            .filterDate(window_start, window_end)
            .aggregate_array("system:index")
        )
        collection = collection.filter(ee.Filter.inList("system:index", dw_ids))

    rows = _daily_stats(collection, geometry, band="SCL", with_cloud=True)
    if not rows:
        raise ValueError(
            f"No Sentinel-2 scenes with <= {max_cloud_pct}% cloud found between "
            f"{window_start} and {window_end} over the KML area"
            + (" (with matching Dynamic World image)." if require_dynamic_world else ".")
        )

    covered = [r for r in rows if r["aoi_coverage_percent"] >= MIN_AOI_COVERAGE_PCT] or rows
    chosen = next(
        (r for r in covered if r["aoi_cloud_percent"] <= clear_aoi_cloud_pct), None
    )
    reason = "nearest_clear_date"
    if chosen is None:
        # rows are newest-first, so min() keeps the nearest date on ties
        chosen = min(covered, key=lambda r: r["aoi_cloud_percent"])
        reason = "least_cloudy_date_in_window"

    return {
        "sensor": "Sentinel-2",
        "selected_date": chosen["date"],
        "start_date": chosen["date"],
        "end_date": _next_day(chosen["date"]),
        "selection": reason,
        "aoi_cloud_percent": chosen["aoi_cloud_percent"],
        "aoi_coverage_percent": chosen["aoi_coverage_percent"],
        "tile_cloud_percent": chosen["tile_cloud_percent"],
        "window_start": window_start,
        "window_end": (date.fromisoformat(window_end) - timedelta(days=1)).isoformat(),
        "window_days": window_days,
        "max_tile_cloud_percent": max_cloud_pct,
        "clear_aoi_cloud_threshold_percent": clear_aoi_cloud_pct,
        "candidates": rows,
    }


def select_recent_s1_date(
    geometry: ee.Geometry,
    *,
    today: Optional[date] = None,
    window_days: int = CLEAR_WINDOW_DAYS,
    polarisation: str = "VV",
) -> Dict[str, Any]:
    """Nearest-to-today Sentinel-1 IW date (covering the AOI) inside the window."""
    window_start, window_end = recent_window(today, window_days)
    collection = (
        ee.ImageCollection(S1_COLLECTION)
        .filterBounds(geometry)
        .filterDate(window_start, window_end)
        .filter(ee.Filter.listContains("transmitterReceiverPolarisation", polarisation))
        .filter(ee.Filter.eq("instrumentMode", "IW"))
    )
    rows = _daily_stats(collection, geometry, band=polarisation, with_cloud=False)
    if not rows:
        raise ValueError(
            f"No Sentinel-1 {polarisation} image found between {window_start} and "
            f"{window_end} over the KML area."
        )

    covered = [r for r in rows if r["aoi_coverage_percent"] >= MIN_AOI_COVERAGE_PCT]
    if covered:
        chosen, reason = covered[0], "nearest_date_covering_aoi"
    else:
        chosen = max(rows, key=lambda r: r["aoi_coverage_percent"])
        reason = "best_coverage_date_in_window"

    return {
        "sensor": "Sentinel-1",
        "selected_date": chosen["date"],
        "start_date": chosen["date"],
        "end_date": _next_day(chosen["date"]),
        "selection": reason,
        "aoi_coverage_percent": chosen["aoi_coverage_percent"],
        "window_start": window_start,
        "window_end": (date.fromisoformat(window_end) - timedelta(days=1)).isoformat(),
        "window_days": window_days,
        "candidates": rows,
    }
