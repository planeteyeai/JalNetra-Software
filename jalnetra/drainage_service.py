"""
jalnetra.drainage_service — MERIT Hydro drainage extraction for JalNetra API.

Wraps drainage_core (from merit_drainage) without changing its algorithm.
Produces main-stream and sub-stream (upstreams) KMLs from a river polygon KML.
"""
from __future__ import annotations

import os
from typing import Any, Callable, Dict, Optional

from jalnetra.drainage_core import (
    MERIT_ASSET,
    NODATA,
    RES,
    DrainageError,
    EarthEngineSource,
    Grid,
    Params,
    Result,
    run_pipeline,
    safe_filename,
)


class _PreInitializedEarthEngineSource(EarthEngineSource):
    """Same MERIT fetch as EarthEngineSource, but assumes ee is already initialized."""

    def __init__(self) -> None:
        try:
            import ee
        except ImportError as exc:
            raise DrainageError(
                "earthengine-api is not installed: pip install earthengine-api",
                503,
            ) from exc
        self.ee = ee
        self.image = (
            ee.Image(MERIT_ASSET)
            .select(["upa", "dir", "elv"])
            .toFloat()
            .unmask(NODATA)
        )


def _default_params() -> Params:
    return Params(
        threshold_km2=float(os.getenv("DRAINAGE_THRESHOLD_KM2", "1")),
        buffer_m=float(os.getenv("DRAINAGE_BUFFER_M", "2000")),
        snap_m=float(os.getenv("DRAINAGE_SNAP_M", "150")),
    )


def _source_factory(river) -> Any:
    if os.getenv("DRAINAGE_SOURCE", "earthengine").lower() == "synthetic":
        from jalnetra.drainage_synthetic import SyntheticSource

        return SyntheticSource(river)
    return _PreInitializedEarthEngineSource()


def analyze_drainage(
    kml_bytes: bytes,
    filename: str = "river.kml",
    *,
    source_factory: Optional[Callable] = None,
) -> Dict[str, Any]:
    """
    Polygon KML → main stream + sub stream (upstreams) KML bytes + summary.

    Sub-stream KML = merit_drainage upstreams.kml (one placemark per tributary).
    """
    if not kml_bytes:
        raise DrainageError("The uploaded file is empty.", 400)
    params = _default_params()
    factory = source_factory or _source_factory
    result: Result = run_pipeline(kml_bytes, filename, params, factory)
    base = safe_filename(result.river_name)
    return {
        "river_name": result.river_name,
        "summary": result.summary,
        "main_stream_kml_bytes": result.main_kml.encode("utf-8"),
        "sub_stream_kml_bytes": result.upstreams_kml.encode("utf-8"),
        "main_stream_kml_filename": f"{base}_main_stream.kml",
        "sub_stream_kml_filename": f"{base}_sub_stream.kml",
        "geojson": result.geojson,
    }


# Re-export for callers that need the exception type / grid helpers.
__all__ = [
    "DrainageError",
    "Grid",
    "Params",
    "RES",
    "analyze_drainage",
    "safe_filename",
]
