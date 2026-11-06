#!/usr/bin/env python3
"""
api_main.py — Unified JalNetra REST API.

Endpoints:
  POST /api/flood-water           — KML → flood/water datewise (fixed 1 Jun 2026→today)
  POST /api/flood-water-dated     — KML + start/end dates → same flood buffer logic
  POST /api/main-bod-cod          — stream KML → daily BOD/COD (Landsat Wang + S2 COD)
  POST /api/streams-bod-cod       — joining/sub-stream KML → per-stream BOD/COD + pixels
  POST /api/drainage              — polygon KML → main-stream + sub-stream KMLs

Usage:
    python api_main.py
    EE_SERVICE_ACCOUNT_JSON='...' python api_main.py   # flood endpoint needs GEE

Flood logic reuses ../flood_tile (Sentinel-1 SAR).
Main/streams BOD/COD uses Landsat 8/9 TOA + Sentinel-2 (GEE).
Vegetation type/health uses Sentinel-2 + Dynamic World (GEE).
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import socket
import sys
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from dotenv import load_dotenv

load_dotenv()

import uvicorn
from cachetools import TTLCache
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import HTMLResponse, Response

HERE = Path(__file__).resolve().parent
FLOOD_TILE_DIR = HERE.parent / "flood_tile"
sys.path.insert(0, str(HERE))
if FLOOD_TILE_DIR.is_dir():
    sys.path.insert(0, str(FLOOD_TILE_DIR))

from jalnetra.bank_erosion_service import (
    analyze_bank_erosion,
    bank_erosion_geometry,
)
from jalnetra.bod_cod_service import analyze_main_bod_cod, analyze_streams_bod_cod
from jalnetra.drainage_service import DrainageError, analyze_drainage
from jalnetra.fabdem_raw_service import download_fabdem_dtm_raw_from_kml
from jalnetra.fabdem_service import download_fabdem_dtm_from_kml
from jalnetra.fishing_point_service import (
    analyze_fishing_points,
    fishing_point_geometry,
)
from jalnetra.kml_buffer import buffered_analysis_geometry
from jalnetra.kml_pixel_smoother import smooth_kml_bytes
from jalnetra.lithology_service import analyze_lithology, lithology_geometry
from jalnetra.lulc_service import analyze_lulc, lulc_analysis_geometry
from jalnetra.salinity_service import analyze_salinity, salinity_geometry
from jalnetra.satellite_wq_service import (
    MAX_CLOUD_S2 as SAT_WQ_MAX_CLOUD,
    analyze_coliform,
    analyze_eutrophication,
    analyze_nitrogen,
    satellite_wq_geometry,
)
from jalnetra.scene_dates import recent_window, select_clear_s2_date
from jalnetra.silt_service import analyze_silt, silt_geometry
from jalnetra.vegetation_service import (
    analyze_vegetation_health,
    analyze_vegetation_type,
)
from jalnetra.water_depth_service import analyze_live_water_depth
from jalnetra.water_quality_service import (
    analyze_water_quality,
    water_quality_geometry,
)

_EXCEL_CACHE: TTLCache = TTLCache(maxsize=200, ttl=3600)
# KML download links stay valid for 30 minutes after the API response is built.
_KML_TTL_S = 30 * 60
_KML_CACHE: TTLCache = TTLCache(maxsize=200, ttl=_KML_TTL_S)
_TIF_CACHE: TTLCache = TTLCache(maxsize=50, ttl=3600)
# Disk-backed KML store — survives uvicorn --reload (in-memory cache alone does not).
_KML_DISK_DIR = HERE / ".kml_cache"
_KML_DISK_DIR.mkdir(exist_ok=True)
_KML_DISK_TTL_S = _KML_TTL_S


def _init_earth_engine() -> None:
    import ee

    raw = os.environ.get("EE_SERVICE_ACCOUNT_JSON")
    if not raw:
        raise ValueError(
            "EE_SERVICE_ACCOUNT_JSON env var is required for flood-water analysis. "
            "Set it to your Google Earth Engine service account JSON."
        )
    service_account = raw if isinstance(raw, str) else json.dumps(raw)
    project = os.environ.get("EE_PROJECT_ID")
    if not project:
        try:
            parsed = json.loads(service_account)
            if isinstance(parsed, dict):
                project = parsed.get("project_id")
        except json.JSONDecodeError:
            project = None

    init_kwargs: Dict[str, Any] = {
        "credentials": ee.ServiceAccountCredentials(None, key_data=service_account),
    }
    if project:
        init_kwargs["project"] = project
    ee.Initialize(**init_kwargs)


_ee_ready = False
_ee_error: str | None = None


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global _ee_ready, _ee_error
    try:
        _init_earth_engine()
        _ee_ready = True
        _ee_error = None
    except Exception as exc:
        _ee_ready = False
        _ee_error = str(exc)
    yield


app = FastAPI(
    title="JalNetra API",
    description="Flood/water detection and BOD/COD estimation from KML upload",
    version="1.0.0",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
)

# Swagger "Try it out" sends this header so ngrok free tier skips the warning page.
# Must be injected as real JS — swagger_ui_parameters JSON-encodes values, so a
# string interceptor becomes a string and Swagger fails with
# "requestInterceptor is not a function".
_NGROK_SWAGGER_INTERCEPTOR_JS = (
    "requestInterceptor: (req) => {"
    " req.headers['ngrok-skip-browser-warning'] = 'true';"
    " return req; },"
)


@app.get("/docs", include_in_schema=False)
async def swagger_docs() -> HTMLResponse:
    html = get_swagger_ui_html(
        openapi_url=app.openapi_url,
        title=f"{app.title} - Swagger UI",
        swagger_ui_parameters={
            "persistAuthorization": True,
            # Large multi-chainage payloads can crash Swagger's highlighter
            # ("Could not render responses_Responses").
            "syntaxHighlight": {"activated": False},
            "defaultModelsExpandDepth": -1,
        },
    )
    body = html.body.decode("utf-8")
    # Insert as a live function before the presets block.
    body = body.replace(
        "presets: [",
        f"{_NGROK_SWAGGER_INTERCEPTOR_JS}\n    presets: [",
        1,
    )
    return HTMLResponse(content=body)


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _geojson_centroid(
    geojson: Dict[str, Any],
) -> Tuple[Optional[float], Optional[float]]:
    """Return (latitude, longitude) from GeoJSON polygon geometry."""
    if not geojson:
        return None, None
    points: List[List[float]] = []
    geom_type = geojson.get("type")
    coords = geojson.get("coordinates") or []
    if geom_type == "Polygon":
        for lon, lat in coords[0]:
            points.append([lon, lat])
    elif geom_type == "MultiPolygon":
        for poly in coords:
            for lon, lat in poly[0]:
                points.append([lon, lat])
    if not points:
        return None, None
    lon = sum(p[0] for p in points) / len(points)
    lat = sum(p[1] for p in points) / len(points)
    return round(lat, 6), round(lon, 6)


def _public_url(request: Request, path: str) -> str:
    base = str(request.base_url).rstrip("/")
    return f"{base}{path}"


def _network_host() -> str:
    """LAN IP for sharing — set PUBLIC_HOST=192.168.x.x to override."""
    explicit = os.environ.get("PUBLIC_HOST", "").strip()
    if explicit:
        return explicit
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"


@app.get("/")
async def root() -> Dict[str, Any]:
    return {
        "message": "JalNetra API",
        "version": "1.0.0",
        "docs": "/docs",
        "endpoints": (
            "POST /api/flood-water, POST /api/flood-water-dated, "
            "POST /api/main-bod-cod, POST /api/streams-bod-cod, POST /api/drainage, "
            "POST /api/vegetation-type, POST /api/vegetation-health, "
            "POST /api/lulc, POST /api/salinity, POST /api/bank-erosion, "
            "POST /api/water-quality, POST /api/eutrophication, POST /api/nitrogen, "
            "POST /api/coliform, POST /api/water-depth, POST /api/lithology, "
            "POST /api/silt-classification, POST /api/silt-volume, "
            "POST /api/fishing-point, POST /api/fabdem-dtm, "
            "POST /api/fabdem-dtm-raw, POST /api/copernicus-dsm, "
            "POST /api/all (one KML -> every API)"
        ),
        "ngrok_free_tier": (
            "Browser: click 'Visit Site' once on the ngrok warning page, then use /docs. "
            "Postman/curl: add header ngrok-skip-browser-warning: true on every request."
        ),
    }


@app.get("/health")
async def health() -> Dict[str, Any]:
    return {
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "earth_engine": _ee_ready,
        "earth_engine_error": _ee_error,
    }


def _flood_water_response(request: Request, result: Dict[str, Any]) -> Dict[str, Any]:
    """Attach one KML download URL per image date."""
    datewise_out: List[Dict[str, Any]] = []
    for entry in result.get("datewise") or []:
        row = {k: v for k, v in entry.items() if k != "kml_bytes"}
        kml_id = uuid.uuid4().hex
        _put_cached_kml(kml_id, entry["kml_bytes"])
        row["kml_id"] = kml_id
        row["kml_download_url"] = _public_url(request, f"/api/flood-water/kml/{kml_id}")
        datewise_out.append(row)
    result["datewise"] = datewise_out
    return result


FLOOD_DEFAULT_START_DATE = "2026-06-01"


async def _run_flood_water_analysis(
    request: Request,
    kml_bytes: bytes,
    start_date: str,
    end_date: str,
) -> Dict[str, Any]:
    """Shared flood runner: permanent water 50 m border; flood red 100 m centerline."""
    try:
        from jalnetra.flood_deps.kml_utils import (
            parse_kml_plots,
            plots_to_combined_geometry,
        )
        from jalnetra.flood_water_service import analyze_flood_water_datewise
    except ImportError as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Flood modules could not be imported: {exc}",
        ) from exc

    plots = parse_kml_plots(kml_bytes)
    geometry = plots_to_combined_geometry(plots)

    try:
        result = await asyncio.to_thread(
            analyze_flood_water_datewise,
            geometry,
            start_date,
            end_date,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Earth Engine analysis failed: {exc}",
        ) from exc

    return _flood_water_response(request, result)


@app.post("/api/flood-water")
async def flood_water(
    request: Request,
    kml: UploadFile = File(..., description="KML file with region boundary"),
) -> Dict[str, Any]:
    """
    Upload KML → per-image flood/water areas and smoothed KMLs.

    No date input: fixed range 1 June 2026 → today.
    Permanent water: 50 m border buffer (unchanged).
    Flood (red): 100 m lateral buffer from KML centerline.
    Pre/post dates follow Sentinel-1 image sequence.
    """
    _require_earth_engine()

    start_date = FLOOD_DEFAULT_START_DATE
    end_date = datetime.now().strftime("%Y-%m-%d")
    if start_date >= end_date:
        raise HTTPException(
            status_code=400,
            detail=f"Flood range start {start_date} must be before today ({end_date}).",
        )

    kml_bytes = await kml.read()
    if not kml_bytes:
        raise HTTPException(status_code=400, detail="KML file is empty.")

    return await _run_flood_water_analysis(request, kml_bytes, start_date, end_date)


@app.post("/api/flood-water-dated")
async def flood_water_dated(
    request: Request,
    kml: UploadFile = File(..., description="KML file with region boundary"),
    start_date: str = Form(..., description="Start date YYYY-MM-DD"),
    end_date: str = Form(..., description="End date YYYY-MM-DD"),
) -> Dict[str, Any]:
    """
    Upload KML + start/end dates → flood/water areas and smoothed KMLs.

    Same logic as POST /api/flood-water:
      - permanent water (blue): 50 m border buffer
      - flood (red): 100 m lateral buffer from KML centerline
    Caller supplies the Sentinel-1 analysis date range.
    """
    _require_earth_engine()

    try:
        datetime.strptime(start_date, "%Y-%m-%d")
        datetime.strptime(end_date, "%Y-%m-%d")
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail="start_date and end_date must be YYYY-MM-DD.",
        ) from exc

    if start_date >= end_date:
        raise HTTPException(
            status_code=400,
            detail=f"start_date {start_date} must be before end_date {end_date}.",
        )

    kml_bytes = await kml.read()
    if not kml_bytes:
        raise HTTPException(status_code=400, detail="KML file is empty.")

    return await _run_flood_water_analysis(request, kml_bytes, start_date, end_date)


@app.get("/api/flood-water/kml/{kml_id}")
async def download_flood_water_kml(kml_id: str) -> Response:
    kml_bytes = _get_cached_kml(kml_id)
    if kml_bytes is None:
        raise HTTPException(
            status_code=404,
            detail=(
                "KML not found or expired. "
                "Run POST /api/flood-water or POST /api/flood-water-dated again."
            ),
        )
    return Response(
        content=kml_bytes,
        media_type="application/vnd.google-earth.kml+xml",
        headers={"Content-Disposition": 'attachment; filename="flood_water.kml"'},
    )


def _main_bod_cod_response(request: Request, result: Dict[str, Any]) -> Dict[str, Any]:
    kml_bytes = result.pop("kml_bytes")
    kml_id = uuid.uuid4().hex
    _put_cached_kml(kml_id, kml_bytes)
    result["kml_id"] = kml_id
    result["kml_download_url"] = _public_url(
        request, f"/api/main-bod-cod/kml/{kml_id}"
    )
    return result


@app.post("/api/main-bod-cod")
async def main_bod_cod(
    request: Request,
    kml: UploadFile = File(
        ..., description="Main-stream / joining-stream KML (LineString placemarks)"
    ),
) -> Dict[str, Any]:
    """
    Upload stream KML → daily BOD/COD at 10 m chainage.

    BOD from Landsat 8+9 TOA (Wang + local calibration).
    COD from BOD×ratio + Sentinel-2 TSS/NDCI correction.
    One value per Landsat acquisition day (not hourly). Returns per-stream
    mean/min/max, daily chainage series, and a downloadable pixel KML.
    """
    _require_earth_engine()

    kml_bytes = await kml.read()
    if not kml_bytes:
        raise HTTPException(status_code=400, detail="KML file is empty.")

    try:
        result = await asyncio.to_thread(analyze_main_bod_cod, kml_bytes)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Main BOD/COD analysis failed: {exc}",
        ) from exc

    return await asyncio.to_thread(_main_bod_cod_response, request, result)


@app.get("/api/main-bod-cod/kml/{kml_id}")
async def download_main_bod_cod_kml(kml_id: str) -> Response:
    kml_bytes = _get_cached_kml(kml_id)
    if kml_bytes is None:
        raise HTTPException(
            status_code=404,
            detail="KML not found or expired. Run POST /api/main-bod-cod again.",
        )
    return Response(
        content=kml_bytes,
        media_type="application/vnd.google-earth.kml+xml",
        headers={
            "Content-Disposition": 'attachment; filename="main_bod_cod_pixels.kml"'
        },
    )


def _streams_bod_cod_response(request: Request, result: Dict[str, Any]) -> Dict[str, Any]:
    kml_bytes = result.pop("kml_bytes")
    kml_id = uuid.uuid4().hex
    _put_cached_kml(kml_id, kml_bytes)
    result["kml_id"] = kml_id
    result["kml_download_url"] = _public_url(
        request, f"/api/streams-bod-cod/kml/{kml_id}"
    )
    return result


@app.post("/api/streams-bod-cod")
async def streams_bod_cod(
    request: Request,
    kml: UploadFile = File(
        ..., description="Joining / sub-stream KML (LineString placemarks)"
    ),
) -> Dict[str, Any]:
    """
    Upload joining-stream KML → per-stream BOD/COD + every pixel lat/lon.

    Same Landsat Wang BOD + Sentinel-2 COD correction as main, with a
    relaxed water mask for narrow streams. Returns mean/min/max per stream
    and a pixel table (stream, chainage, lat, lon, BOD, COD).
    """
    _require_earth_engine()

    kml_bytes = await kml.read()
    if not kml_bytes:
        raise HTTPException(status_code=400, detail="KML file is empty.")

    try:
        result = await asyncio.to_thread(analyze_streams_bod_cod, kml_bytes)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Streams BOD/COD analysis failed: {exc}",
        ) from exc

    return await asyncio.to_thread(_streams_bod_cod_response, request, result)


@app.get("/api/streams-bod-cod/kml/{kml_id}")
async def download_streams_bod_cod_kml(kml_id: str) -> Response:
    kml_bytes = _get_cached_kml(kml_id)
    if kml_bytes is None:
        raise HTTPException(
            status_code=404,
            detail="KML not found or expired. Run POST /api/streams-bod-cod again.",
        )
    return Response(
        content=kml_bytes,
        media_type="application/vnd.google-earth.kml+xml",
        headers={
            "Content-Disposition": 'attachment; filename="streams_bod_cod_pixels.kml"'
        },
    )


def _store_kml(kml_bytes: bytes) -> str:
    """Cache raw KML bytes (line/polygon layers; no GroundOverlay smoothing)."""
    kml_id = uuid.uuid4().hex
    _put_cached_kml(kml_id, kml_bytes if isinstance(kml_bytes, bytes) else bytes(kml_bytes))
    return kml_id


@app.post("/api/drainage")
async def drainage(
    request: Request,
    kml: UploadFile = File(..., description="River polygon KML / KMZ"),
) -> Dict[str, Any]:
    """
    Upload river polygon KML → main-stream + sub-stream KMLs (MERIT Hydro).

    Returns downloadable KML ids for each layer. For BOD/COD: upload the
    main-stream KML to POST /api/main-bod-cod, or sub-stream KML to
    POST /api/streams-bod-cod.
    """
    if os.getenv("DRAINAGE_SOURCE", "earthengine").lower() != "synthetic":
        _require_earth_engine()
    kml_bytes = await kml.read()
    if not kml_bytes:
        raise HTTPException(status_code=400, detail="KML file is empty.")

    try:
        result = await asyncio.to_thread(
            analyze_drainage, kml_bytes, kml.filename or "river.kml"
        )
    except DrainageError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500, detail=f"Drainage analysis failed: {exc}"
        ) from exc

    main_id = _store_kml(result.pop("main_stream_kml_bytes"))
    sub_id = _store_kml(result.pop("sub_stream_kml_bytes"))
    result.pop("geojson", None)
    result["main_stream"] = {
        "kml_id": main_id,
        "kml_download_url": _public_url(request, f"/api/drainage/kml/{main_id}"),
        "kml_filename": result.pop(
            "main_stream_kml_filename", "main_stream.kml"
        ),
        "kind": "main_stream",
    }
    result["sub_stream"] = {
        "kml_id": sub_id,
        "kml_download_url": _public_url(request, f"/api/drainage/kml/{sub_id}"),
        "kml_filename": result.pop(
            "sub_stream_kml_filename", "sub_stream.kml"
        ),
        "kind": "sub_stream",
    }
    return result


@app.get(
    "/api/drainage/kml/{kml_id}",
    response_class=Response,
    responses={
        200: {
            "description": "Drainage KML (main stream or sub stream)",
            "content": {
                "application/vnd.google-earth.kml+xml": {
                    "schema": {"type": "string", "format": "binary"}
                }
            },
        },
        404: {"description": "KML not found or expired"},
    },
)
async def download_drainage_kml(kml_id: str) -> Response:
    kml_bytes = _get_cached_kml(kml_id)
    if kml_bytes is None:
        raise HTTPException(
            status_code=404,
            detail="KML not found or expired. Run POST /api/drainage again.",
        )
    return Response(
        content=kml_bytes,
        media_type="application/vnd.google-earth.kml+xml",
        headers={"Content-Disposition": 'attachment; filename="drainage.kml"'},
    )


def _require_earth_engine() -> None:
    if not _ee_ready:
        raise HTTPException(
            status_code=503,
            detail=(
                "Earth Engine not initialized. Set EE_SERVICE_ACCOUNT_JSON "
                "and restart the server."
            ),
        )


def _kml_geometry_from_bytes(kml_bytes: bytes):
    from jalnetra.flood_deps.kml_utils import kml_bytes_to_ee_geometry

    return kml_bytes_to_ee_geometry(kml_bytes)


def _kml_disk_path(kml_id: str) -> Path:
    # Reject path traversal — ids are always hex from uuid4.
    safe = "".join(c for c in kml_id if c.isalnum())
    return _KML_DISK_DIR / f"{safe}.kml"


def _put_cached_kml(kml_id: str, kml_bytes: bytes) -> None:
    _KML_CACHE[kml_id] = kml_bytes
    try:
        _kml_disk_path(kml_id).write_bytes(kml_bytes)
    except OSError:
        pass


def _get_cached_kml(kml_id: str) -> Optional[bytes]:
    cached = _KML_CACHE.get(kml_id)
    if cached is not None:
        return cached
    path = _kml_disk_path(kml_id)
    try:
        if not path.is_file():
            return None
        age = time.time() - path.stat().st_mtime
        if age > _KML_DISK_TTL_S:
            try:
                path.unlink()
            except OSError:
                pass
            return None
        data = path.read_bytes()
        _KML_CACHE[kml_id] = data
        return data
    except OSError:
        return None


def _store_smoothed_kml(kml_bytes: bytes) -> str:
    """Pixel-smooth GroundOverlay PNGs (kml_pixel_smoother) and cache the KML."""
    kml_id = uuid.uuid4().hex
    try:
        payload = smooth_kml_bytes(kml_bytes)
    except Exception:
        payload = kml_bytes
    _put_cached_kml(kml_id, payload)
    return kml_id


def _vegetation_response(
    request: Request,
    result: Dict[str, Any],
    *,
    prefix: str,
    filename: str,
    smooth: bool = True,
) -> Dict[str, Any]:
    if smooth:
        kml_id = _store_smoothed_kml(result.pop("kml_bytes"))
    else:
        kml_id = uuid.uuid4().hex
        _put_cached_kml(kml_id, result.pop("kml_bytes"))
    result["kml_id"] = kml_id
    result["kml_download_url"] = _public_url(request, f"/api/{prefix}/kml/{kml_id}")
    result["kml_filename"] = filename
    return result


def _water_quality_response(request: Request, result: Dict[str, Any]) -> Dict[str, Any]:
    """Attach four KML download URLs (WST, TSS, NDWI, NDCI)."""
    layers = {
        "wst": ("wst_kml_bytes", "wst.kml"),
        "tss": ("tss_kml_bytes", "tss_turbidity.kml"),
        "ndwi": ("ndwi_kml_bytes", "ndwi_permanent_water.kml"),
        "ndci": ("ndci_kml_bytes", "ndci_chlorophyll.kml"),
    }
    for layer_key, (bytes_key, filename) in layers.items():
        kml_id = _store_smoothed_kml(result.pop(bytes_key))
        result[layer_key]["kml_id"] = kml_id
        result[layer_key]["kml_download_url"] = _public_url(
            request, f"/api/water-quality/kml/{kml_id}"
        )
        result[layer_key]["kml_filename"] = filename
    return result


def _store_raw_kml(kml_bytes: bytes) -> str:
    """Cache KML as-is (silt already EE-smoothed; pixel smoother was too slow)."""
    kml_id = uuid.uuid4().hex
    _put_cached_kml(kml_id, kml_bytes)
    return kml_id


def _silt_classification_response(
    request: Request, result: Dict[str, Any]
) -> Dict[str, Any]:
    """Attach classification KML download URLs only (no volume KMLs)."""
    for key in list(result.get("months") or {}):
        result.pop(f"{key}_volume_kml_bytes", None)
        result["months"][key].pop("silt_volume", None)
        bytes_key = f"{key}_kml_bytes"
        if bytes_key not in result:
            continue
        filename = result["months"][key].get("kml_filename", f"silt_{key}.kml")
        kml_id = _store_smoothed_kml(result.pop(bytes_key))
        result["months"][key]["kml_id"] = kml_id
        result["months"][key]["kml_download_url"] = _public_url(
            request, f"/api/silt/kml/{kml_id}"
        )
        result["months"][key]["kml_filename"] = filename
    for row in result.get("combined_summary") or []:
        row.pop("total_silt_volume_m3", None)
    return result


def _silt_volume_response(request: Request, result: Dict[str, Any]) -> Dict[str, Any]:
    """Attach volume KML download URLs only (no classification KMLs)."""
    for key in list(result.get("months") or {}):
        result.pop(f"{key}_kml_bytes", None)
        month = result["months"][key]
        for drop in ("categories", "legend", "kml_filename", "export_scale_m"):
            month.pop(drop, None)
        volume_key = f"{key}_volume_kml_bytes"
        volume = month.get("silt_volume")
        if volume_key in result and volume is not None:
            volume_id = _store_smoothed_kml(result.pop(volume_key))
            volume["kml_id"] = volume_id
            volume["kml_download_url"] = _public_url(
                request, f"/api/silt/kml/{volume_id}"
            )
    result.pop("legend", None)
    return result


def _water_depth_response(request: Request, result: Dict[str, Any]) -> Dict[str, Any]:
    """Attach live-water (blue) and depth KML download URLs."""
    water_id = _store_smoothed_kml(result.pop("water_kml_bytes"))
    depth_id = _store_smoothed_kml(result.pop("depth_kml_bytes"))
    live_water = dict(result.get("live_water") or {})
    live_water.update(
        {
            "kml_id": water_id,
            "kml_download_url": _public_url(
                request, f"/api/water-depth/kml/{water_id}"
            ),
            "kml_filename": result.pop("water_kml_filename", "live_water.kml"),
            "color": "#0000FF",
        }
    )
    result["live_water"] = live_water
    result["depth"]["kml_id"] = depth_id
    result["depth"]["kml_download_url"] = _public_url(
        request, f"/api/water-depth/kml/{depth_id}"
    )
    result["depth"]["kml_filename"] = result.pop(
        "depth_kml_filename", "water_depth.kml"
    )
    return result


def _lulc_response(request: Request, result: Dict[str, Any]) -> Dict[str, Any]:
    """Attach six yearly LULC KML download URLs (2021–2026)."""
    for year in (2021, 2022, 2023, 2024, 2025, 2026):
        bytes_key = f"y{year}_kml_bytes"
        year_key = str(year)
        if bytes_key not in result:
            continue
        filename = result["years"][year_key].get("kml_filename", f"lulc_{year}.kml")
        kml_id = _store_smoothed_kml(result.pop(bytes_key))
        result["years"][year_key]["kml_id"] = kml_id
        result["years"][year_key]["kml_download_url"] = _public_url(
            request, f"/api/lulc/kml/{kml_id}"
        )
        result["years"][year_key]["kml_filename"] = filename
    return result


def _vegetation_geometry(kml_bytes: bytes):
    """Rectangular AOI: KML bounds + 2 km buffer on each side."""
    return buffered_analysis_geometry(kml_bytes)


@app.post("/api/vegetation-type")
async def vegetation_type(
    request: Request,
    kml: UploadFile = File(..., description="KML AOI boundary"),
) -> Dict[str, Any]:
    """
    Upload KML → vegetation type map inside a rectangle (KML bounds + 2 km buffer).

    No date input: uses the nearest clear Sentinel-2 date in the last 30 days.
    Returns area (ha) and % of analysis area per class, plus downloadable KML overlay.
    """
    _require_earth_engine()

    kml_bytes = await kml.read()
    if not kml_bytes:
        raise HTTPException(status_code=400, detail="KML file is empty.")

    try:
        analysis_geom, _input_geom, buffer_info = await asyncio.to_thread(
            _vegetation_geometry, kml_bytes
        )
        scene = await asyncio.to_thread(
            select_clear_s2_date, analysis_geom, require_dynamic_world=True
        )
        result = await asyncio.to_thread(
            analyze_vegetation_type,
            analysis_geom,
            scene["start_date"],
            scene["end_date"],
            buffer_info=buffer_info,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500, detail=f"Vegetation type analysis failed: {exc}"
        ) from exc

    result["scene_selection"] = scene
    return await asyncio.to_thread(
        _vegetation_response,
        request,
        result,
        prefix="vegetation-type",
        filename="vegetation_type.kml",
    )


@app.get("/api/vegetation-type/kml/{kml_id}")
async def download_vegetation_type_kml(kml_id: str) -> Response:
    kml_bytes = _get_cached_kml(kml_id)
    if kml_bytes is None:
        raise HTTPException(
            status_code=404,
            detail="KML not found or expired. Run POST /api/vegetation-type again.",
        )
    return Response(
        content=kml_bytes,
        media_type="application/vnd.google-earth.kml+xml",
        headers={"Content-Disposition": 'attachment; filename="vegetation_type.kml"'},
    )


@app.post("/api/vegetation-health")
async def vegetation_health(
    request: Request,
    kml: UploadFile = File(..., description="KML AOI boundary"),
) -> Dict[str, Any]:
    """
    Upload KML → vegetation health map inside a rectangle (KML bounds + 2 km buffer).

    No date input: uses the nearest clear Sentinel-2 date in the last 30 days.
    Returns area (ha) and % of total vegetation per health class, plus downloadable KML.
    """
    _require_earth_engine()

    kml_bytes = await kml.read()
    if not kml_bytes:
        raise HTTPException(status_code=400, detail="KML file is empty.")

    try:
        analysis_geom, _input_geom, buffer_info = await asyncio.to_thread(
            _vegetation_geometry, kml_bytes
        )
        scene = await asyncio.to_thread(
            select_clear_s2_date, analysis_geom, require_dynamic_world=True
        )
        result = await asyncio.to_thread(
            analyze_vegetation_health,
            analysis_geom,
            scene["start_date"],
            scene["end_date"],
            buffer_info=buffer_info,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500, detail=f"Vegetation health analysis failed: {exc}"
        ) from exc

    result["scene_selection"] = scene
    return await asyncio.to_thread(
        _vegetation_response,
        request,
        result,
        prefix="vegetation-health",
        filename="vegetation_health.kml",
    )


@app.get("/api/vegetation-health/kml/{kml_id}")
async def download_vegetation_health_kml(kml_id: str) -> Response:
    kml_bytes = _get_cached_kml(kml_id)
    if kml_bytes is None:
        raise HTTPException(
            status_code=404,
            detail="KML not found or expired. Run POST /api/vegetation-health again.",
        )
    return Response(
        content=kml_bytes,
        media_type="application/vnd.google-earth.kml+xml",
        headers={"Content-Disposition": 'attachment; filename="vegetation_health.kml"'},
    )


@app.post("/api/lulc")
async def lulc(
    request: Request,
    kml: UploadFile = File(..., description="KML AOI boundary"),
) -> Dict[str, Any]:
    """
    Upload KML → annual LULC maps for default years 2021–2026.

    Analysis zone = original AOI + 1 km geodesic buffer.
    Returns class area (ha), accuracy metrics, and six smoothed KML overlays
    (one per year). No year input — years are fixed.
    """
    _require_earth_engine()

    kml_bytes = await kml.read()
    if not kml_bytes:
        raise HTTPException(status_code=400, detail="KML file is empty.")

    try:
        analysis_geom, _input_geom, buffer_info = await asyncio.to_thread(
            lulc_analysis_geometry, kml_bytes
        )
        result = await asyncio.to_thread(
            analyze_lulc,
            analysis_geom,
            buffer_info=buffer_info,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500, detail=f"LULC analysis failed: {exc}"
        ) from exc

    return await asyncio.to_thread(_lulc_response, request, result)


@app.get("/api/lulc/kml/{kml_id}")
async def download_lulc_kml(kml_id: str) -> Response:
    kml_bytes = _get_cached_kml(kml_id)
    if kml_bytes is None:
        raise HTTPException(
            status_code=404,
            detail="KML not found or expired. Run POST /api/lulc again.",
        )
    return Response(
        content=kml_bytes,
        media_type="application/vnd.google-earth.kml+xml",
        headers={"Content-Disposition": 'attachment; filename="lulc.kml"'},
    )


@app.post("/api/salinity")
async def salinity(
    request: Request,
    kml: UploadFile = File(..., description="KML AOI boundary"),
) -> Dict[str, Any]:
    """
    Upload KML → relative salinity index (0–1) over water pixels.

    No date input: uses the nearest clear Sentinel-2 date (≤40% cloud) in the
    last 30 days, with its matching Dynamic World image.
    Returns class-wise salinity range status (JSON) and downloadable KML overlay.
    """
    _require_earth_engine()

    kml_bytes = await kml.read()
    if not kml_bytes:
        raise HTTPException(status_code=400, detail="KML file is empty.")

    try:
        aoi_geom, aoi_info = await asyncio.to_thread(salinity_geometry, kml_bytes)
        scene = await asyncio.to_thread(
            select_clear_s2_date, aoi_geom, require_dynamic_world=True
        )
        result = await asyncio.to_thread(
            analyze_salinity,
            aoi_geom,
            scene["start_date"],
            scene["end_date"],
            aoi_info=aoi_info,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500, detail=f"Salinity analysis failed: {exc}"
        ) from exc

    result["scene_selection"] = scene
    return await asyncio.to_thread(
        _vegetation_response,
        request,
        result,
        prefix="salinity",
        filename="relative_salinity.kml",
    )


@app.get("/api/salinity/kml/{kml_id}")
async def download_salinity_kml(kml_id: str) -> Response:
    kml_bytes = _get_cached_kml(kml_id)
    if kml_bytes is None:
        raise HTTPException(
            status_code=404,
            detail="KML not found or expired. Run POST /api/salinity again.",
        )
    return Response(
        content=kml_bytes,
        media_type="application/vnd.google-earth.kml+xml",
        headers={"Content-Disposition": 'attachment; filename="relative_salinity.kml"'},
    )


@app.post("/api/bank-erosion")
async def bank_erosion(
    request: Request,
    kml: UploadFile = File(..., description="KML AOI boundary (no buffer)"),
) -> Dict[str, Any]:
    """
    Upload KML → bank erosion hotspots for fixed period 2016–2026.

    Landsat 8/9 + MNDWI. No dates input, no buffer.
    Returns class-wise hotspot areas (ha) and downloadable KML overlay.
    """
    _require_earth_engine()

    kml_bytes = await kml.read()
    if not kml_bytes:
        raise HTTPException(status_code=400, detail="KML file is empty.")

    try:
        aoi_geom, aoi_info = await asyncio.to_thread(bank_erosion_geometry, kml_bytes)
        result = await asyncio.to_thread(
            analyze_bank_erosion,
            aoi_geom,
            aoi_info=aoi_info,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500, detail=f"Bank erosion analysis failed: {exc}"
        ) from exc

    return await asyncio.to_thread(
        _vegetation_response,
        request,
        result,
        prefix="bank-erosion",
        filename="bank_erosion_hotspots_2016_2026.kml",
    )


@app.get("/api/bank-erosion/kml/{kml_id}")
async def download_bank_erosion_kml(kml_id: str) -> Response:
    kml_bytes = _get_cached_kml(kml_id)
    if kml_bytes is None:
        raise HTTPException(
            status_code=404,
            detail="KML not found or expired. Run POST /api/bank-erosion again.",
        )
    return Response(
        content=kml_bytes,
        media_type="application/vnd.google-earth.kml+xml",
        headers={
            "Content-Disposition": (
                'attachment; filename="bank_erosion_hotspots_2016_2026.kml"'
            )
        },
    )


@app.post("/api/water-quality")
async def water_quality(
    request: Request,
    kml: UploadFile = File(..., description="KML AOI boundary (no buffer)"),
) -> Dict[str, Any]:
    """
    Upload KML → four water quality KML layers.

    Returns WST, TSS (turbidity), NDWI (permanent water), and NDCI (chlorophyll).
    No date input: Sentinel-2 + Dynamic World use the nearest clear date in the
    last 30 days; Landsat 9 WST uses the median of that same 30-day window.
    No buffer.
    """
    _require_earth_engine()

    kml_bytes = await kml.read()
    if not kml_bytes:
        raise HTTPException(status_code=400, detail="KML file is empty.")

    try:
        aoi_geom, aoi_info = await asyncio.to_thread(water_quality_geometry, kml_bytes)
        scene = await asyncio.to_thread(
            select_clear_s2_date, aoi_geom, require_dynamic_world=True
        )
        result = await asyncio.to_thread(
            analyze_water_quality,
            aoi_geom,
            scene["start_date"],
            scene["end_date"],
            aoi_info=aoi_info,
            landsat_window=recent_window(),
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500, detail=f"Water quality analysis failed: {exc}"
        ) from exc

    result["scene_selection"] = scene
    return await asyncio.to_thread(_water_quality_response, request, result)


@app.get("/api/water-quality/kml/{kml_id}")
async def download_water_quality_kml(kml_id: str) -> Response:
    kml_bytes = _get_cached_kml(kml_id)
    if kml_bytes is None:
        raise HTTPException(
            status_code=404,
            detail="KML not found or expired. Run POST /api/water-quality again.",
        )
    return Response(
        content=kml_bytes,
        media_type="application/vnd.google-earth.kml+xml",
        headers={"Content-Disposition": 'attachment; filename="water_quality.kml"'},
    )


async def _run_satellite_wq(
    request: Request,
    kml: UploadFile,
    *,
    analyze_fn,
    prefix: str,
    filename: str,
    fail_label: str,
) -> Dict[str, Any]:
    """Shared runner: nearest clear S2 (<20% cloud) + pixel-smoothed KML."""
    _require_earth_engine()

    kml_bytes = await kml.read()
    if not kml_bytes:
        raise HTTPException(status_code=400, detail="KML file is empty.")

    try:
        aoi_geom, aoi_info = await asyncio.to_thread(satellite_wq_geometry, kml_bytes)
        # select_clear uses lte; pass just under 20 so selection matches Filter.lt(20).
        scene = await asyncio.to_thread(
            select_clear_s2_date,
            aoi_geom,
            max_cloud_pct=SAT_WQ_MAX_CLOUD - 1e-6,
        )
        result = await asyncio.to_thread(
            analyze_fn,
            aoi_geom,
            scene["start_date"],
            scene["end_date"],
            aoi_info=aoi_info,
            no2_window=recent_window(),
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500, detail=f"{fail_label} analysis failed: {exc}"
        ) from exc

    result["scene_selection"] = scene
    return await asyncio.to_thread(
        _vegetation_response,
        request,
        result,
        prefix=prefix,
        filename=filename,
        smooth=True,
    )


@app.post("/api/eutrophication")
async def eutrophication(
    request: Request,
    kml: UploadFile = File(..., description="KML AOI boundary"),
) -> Dict[str, Any]:
    """
    Upload KML → eutrophication (Carlson TSI trophic class from chlorophyll-a).

    No date input: nearest clear Sentinel-2 date with tile cloud < 20%.
    Returns class-wise areas (JSON) and a pixel-smoothed downloadable KML.
    """
    return await _run_satellite_wq(
        request,
        kml,
        analyze_fn=analyze_eutrophication,
        prefix="eutrophication",
        filename="eutrophication_trophic_class.kml",
        fail_label="Eutrophication",
    )


@app.get("/api/eutrophication/kml/{kml_id}")
async def download_eutrophication_kml(kml_id: str) -> Response:
    kml_bytes = _get_cached_kml(kml_id)
    if kml_bytes is None:
        raise HTTPException(
            status_code=404,
            detail="KML not found or expired. Run POST /api/eutrophication again.",
        )
    return Response(
        content=kml_bytes,
        media_type="application/vnd.google-earth.kml+xml",
        headers={
            "Content-Disposition": (
                'attachment; filename="eutrophication_trophic_class.kml"'
            )
        },
    )


@app.post("/api/nitrogen")
async def nitrogen(
    request: Request,
    kml: UploadFile = File(..., description="KML AOI boundary"),
) -> Dict[str, Any]:
    """
    Upload KML → high-nitrogen risk class (not mg N/L).

    Chlorophyll + WorldCover land sources + Sentinel-5P NO2.
    No date input: nearest clear Sentinel-2 date with tile cloud < 20%.
    Returns class-wise areas (JSON) and a pixel-smoothed downloadable KML.
    """
    return await _run_satellite_wq(
        request,
        kml,
        analyze_fn=analyze_nitrogen,
        prefix="nitrogen",
        filename="nitrogen_risk_class.kml",
        fail_label="Nitrogen",
    )


@app.get("/api/nitrogen/kml/{kml_id}")
async def download_nitrogen_kml(kml_id: str) -> Response:
    kml_bytes = _get_cached_kml(kml_id)
    if kml_bytes is None:
        raise HTTPException(
            status_code=404,
            detail="KML not found or expired. Run POST /api/nitrogen again.",
        )
    return Response(
        content=kml_bytes,
        media_type="application/vnd.google-earth.kml+xml",
        headers={
            "Content-Disposition": 'attachment; filename="nitrogen_risk_class.kml"'
        },
    )


@app.post("/api/coliform")
async def coliform(
    request: Request,
    kml: UploadFile = File(..., description="KML AOI boundary"),
) -> Dict[str, Any]:
    """
    Upload KML → E. coli / coliform bathing class (relative rank).

    SWIR/blue FIB features (Chowdhury 2025) blended with urban pressure.
    No date input: nearest clear Sentinel-2 date with tile cloud < 20%.
    Returns class-wise areas (JSON) and a pixel-smoothed downloadable KML.
    """
    return await _run_satellite_wq(
        request,
        kml,
        analyze_fn=analyze_coliform,
        prefix="coliform",
        filename="coliform_ecoli_class.kml",
        fail_label="Coliform",
    )


@app.get("/api/coliform/kml/{kml_id}")
async def download_coliform_kml(kml_id: str) -> Response:
    kml_bytes = _get_cached_kml(kml_id)
    if kml_bytes is None:
        raise HTTPException(
            status_code=404,
            detail="KML not found or expired. Run POST /api/coliform again.",
        )
    return Response(
        content=kml_bytes,
        media_type="application/vnd.google-earth.kml+xml",
        headers={
            "Content-Disposition": 'attachment; filename="coliform_ecoli_class.kml"'
        },
    )


@app.post("/api/water-depth")
async def water_depth(
    request: Request,
    kml: UploadFile = File(..., description="KML AOI boundary"),
    depth_min_m: Optional[float] = Form(
        None, description="Minimum depth in metres (default: WATER_DEPTH_MIN_M or 1.5)"
    ),
    depth_max_m: Optional[float] = Form(
        None, description="Maximum depth in metres (default: WATER_DEPTH_MAX_M or 2.0)"
    ),
) -> Dict[str, Any]:
    """
    Upload KML → two downloadable KMLs: blue live water (S2 MNDWI frequency)
    and relative depth.

    No date input: live water uses Sentinel-2 MNDWI frequency over the last
    30 days; depth uses the nearest clear Sentinel-2 image in that window.
    Optional min/max depth set the depth scale.
    Response includes the Sentinel-2 image date used for depth.
    """
    _require_earth_engine()

    if (
        depth_min_m is not None
        and depth_max_m is not None
        and depth_max_m <= depth_min_m
    ):
        raise HTTPException(
            status_code=400, detail="depth_max_m must be greater than depth_min_m."
        )

    kml_bytes = await kml.read()
    if not kml_bytes:
        raise HTTPException(status_code=400, detail="KML file is empty.")

    try:
        aoi_geom = await asyncio.to_thread(_kml_geometry_from_bytes, kml_bytes)
        result = await asyncio.to_thread(
            analyze_live_water_depth, aoi_geom, depth_min_m, depth_max_m
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500, detail=f"Water depth analysis failed: {exc}"
        ) from exc

    return await asyncio.to_thread(_water_depth_response, request, result)


@app.get("/api/water-depth/kml/{kml_id}")
async def download_water_depth_kml(kml_id: str) -> Response:
    kml_bytes = _get_cached_kml(kml_id)
    if kml_bytes is None:
        raise HTTPException(
            status_code=404,
            detail="KML not found or expired. Run POST /api/water-depth again.",
        )
    return Response(
        content=kml_bytes,
        media_type="application/vnd.google-earth.kml+xml",
        headers={"Content-Disposition": 'attachment; filename="water_depth.kml"'},
    )


@app.post("/api/lithology")
async def lithology(
    request: Request,
    kml: UploadFile = File(..., description="KML AOI boundary"),
) -> Dict[str, Any]:
    """
    Upload KML → lithological spectral interpretation map.

    No date input: uses the nearest clear Sentinel-2 date in the last 30 days.
    Analysis uses a 50 m buffer from the KML border.
    K-Means clustering, silt class 0 and eight lithology spectral clusters
    (1–8). Returns class areas (ha) and percent of classified area, plus
    downloadable KML overlay.
    """
    _require_earth_engine()

    kml_bytes = await kml.read()
    if not kml_bytes:
        raise HTTPException(status_code=400, detail="KML file is empty.")

    try:
        analysis_geom, _input_geom, buffer_info = await asyncio.to_thread(
            lithology_geometry, kml_bytes
        )
        scene = await asyncio.to_thread(select_clear_s2_date, analysis_geom)
        result = await asyncio.to_thread(
            analyze_lithology,
            analysis_geom,
            scene["start_date"],
            scene["end_date"],
            buffer_info=buffer_info,
            dry_season_only=False,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500, detail=f"Lithology analysis failed: {exc}"
        ) from exc

    result["scene_selection"] = scene
    return _vegetation_response(
        request, result, prefix="lithology", filename="lithology.kml", smooth=False
    )


@app.get("/api/lithology/kml/{kml_id}")
async def download_lithology_kml(kml_id: str) -> Response:
    kml_bytes = _get_cached_kml(kml_id)
    if kml_bytes is None:
        raise HTTPException(
            status_code=404,
            detail="KML not found or expired. Run POST /api/lithology again.",
        )
    return Response(
        content=kml_bytes,
        media_type="application/vnd.google-earth.kml+xml",
        headers={"Content-Disposition": 'attachment; filename="lithology.kml"'},
    )


async def _run_silt_analysis(
    kml: UploadFile,
    *,
    include_classification: bool,
    include_volume: bool,
) -> Dict[str, Any]:
    """Run silt analysis for one product only (skips the other PNG/KML path)."""
    _require_earth_engine()

    kml_bytes = await kml.read()
    if not kml_bytes:
        raise HTTPException(status_code=400, detail="KML file is empty.")

    try:
        aoi_geom, aoi_info = await asyncio.to_thread(silt_geometry, kml_bytes)
        return await asyncio.to_thread(
            analyze_silt,
            aoi_geom,
            aoi_info=aoi_info,
            include_classification=include_classification,
            include_volume=include_volume,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500, detail=f"Silt analysis failed: {exc}"
        ) from exc


@app.post("/api/silt-classification")
async def silt_classification(
    request: Request,
    kml: UploadFile = File(..., description="KML AOI boundary (no buffer)"),
) -> Dict[str, Any]:
    """
    Upload KML → monthly silt classification KMLs from January 2026 through today.

    Classification KMLs only (no silt volume overlays). Months without
    Sentinel-2 imagery are skipped. Dynamic World is not used.
    """
    result = await _run_silt_analysis(
        kml, include_classification=True, include_volume=False
    )
    return await asyncio.to_thread(_silt_classification_response, request, result)


@app.post("/api/silt-volume")
async def silt_volume(
    request: Request,
    kml: UploadFile = File(..., description="KML AOI boundary (no buffer)"),
) -> Dict[str, Any]:
    """
    Upload KML → monthly silt volume KMLs from January 2026 through today.

    Volume KMLs only (no silt classification overlays). Months without
    Sentinel-2 imagery are skipped. Dynamic World is not used.
    """
    result = await _run_silt_analysis(
        kml, include_classification=False, include_volume=True
    )
    return await asyncio.to_thread(_silt_volume_response, request, result)


@app.post("/api/silt")
async def silt(
    request: Request,
    kml: UploadFile = File(..., description="KML AOI boundary (no buffer)"),
) -> Dict[str, Any]:
    """Alias of POST /api/silt-classification (classification KMLs only)."""
    return await silt_classification(request, kml)


@app.get("/api/silt/kml/{kml_id}")
async def download_silt_kml(kml_id: str) -> Response:
    kml_bytes = _get_cached_kml(kml_id)
    if kml_bytes is None:
        raise HTTPException(
            status_code=404,
            detail=(
                "KML not found or expired. Run POST /api/silt-classification "
                "or POST /api/silt-volume again."
            ),
        )
    return Response(
        content=kml_bytes,
        media_type="application/vnd.google-earth.kml+xml",
        headers={"Content-Disposition": 'attachment; filename="silt.kml"'},
    )


@app.post("/api/fishing-point")
async def fishing_point(
    request: Request,
    kml: UploadFile = File(..., description="KML river / AOI boundary"),
) -> Dict[str, Any]:
    """
    Upload KML → robust fishing hotspot points (lat/lon) + one KML.

    Fixed dry windows (Mar–May 2024–2026) and wet windows (Jul–Sep 2023–2025).
    For 2026 dry season, only one Sentinel-2 image is used across Mar–May.
    60 m border buffer. Fish-icon placemarks in the output KML.
    """
    _require_earth_engine()

    kml_bytes = await kml.read()
    if not kml_bytes:
        raise HTTPException(status_code=400, detail="KML file is empty.")

    try:
        aoi_buffered, aoi, aoi_info, access = await asyncio.to_thread(
            fishing_point_geometry, kml_bytes
        )
        result = await asyncio.to_thread(
            analyze_fishing_points,
            aoi_buffered,
            aoi,
            access_points=access or None,
            aoi_info=aoi_info,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500, detail=f"Fishing point analysis failed: {exc}"
        ) from exc

    kml_id = uuid.uuid4().hex
    _put_cached_kml(kml_id, result.pop("kml_bytes"))
    result["kml_id"] = kml_id
    result["kml_download_url"] = _public_url(
        request, f"/api/fishing-point/kml/{kml_id}"
    )
    result["kml_filename"] = result.get("kml_filename", "fishing_points.kml")
    return result


@app.get("/api/fishing-point/kml/{kml_id}")
async def download_fishing_point_kml(kml_id: str) -> Response:
    kml_bytes = _get_cached_kml(kml_id)
    if kml_bytes is None:
        raise HTTPException(
            status_code=404,
            detail="KML not found or expired. Run POST /api/fishing-point again.",
        )
    return Response(
        content=kml_bytes,
        media_type="application/vnd.google-earth.kml+xml",
        headers={"Content-Disposition": 'attachment; filename="fishing_points.kml"'},
    )


@app.post("/api/fabdem-dtm")
async def fabdem_dtm(
    request: Request,
    kml: UploadFile = File(..., description="KML AOI boundary"),
) -> Dict[str, Any]:
    """
    Upload KML → download FABDEM DTM for the KML bbox, clip to exact KML
    geometry, and return a GeoTIFF download URL (open in QGIS).
    """
    kml_bytes = await kml.read()
    if not kml_bytes:
        raise HTTPException(status_code=400, detail="KML file is empty.")

    try:
        result = await asyncio.to_thread(download_fabdem_dtm_from_kml, kml_bytes)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500, detail=f"FABDEM DTM failed: {exc}"
        ) from exc

    tif_id = uuid.uuid4().hex
    _TIF_CACHE[tif_id] = result.pop("tif_bytes")
    result["tif_id"] = tif_id
    result["tif_download_url"] = _public_url(request, f"/api/fabdem-dtm/tif/{tif_id}")
    raw_bytes = result.pop("raw_tif_bytes", None)
    if raw_bytes is not None:
        raw_id = uuid.uuid4().hex
        _TIF_CACHE[raw_id] = raw_bytes
        result["raw_tif_id"] = raw_id
        result["raw_tif_download_url"] = _public_url(
            request, f"/api/fabdem-dtm/tif/{raw_id}"
        )
    return result


@app.get("/api/fabdem-dtm/tif/{tif_id}")
async def download_fabdem_dtm_tif(tif_id: str) -> Response:
    tif_bytes = _TIF_CACHE.get(tif_id)
    if tif_bytes is None:
        raise HTTPException(
            status_code=404,
            detail="TIFF not found or expired. Run POST /api/fabdem-dtm again.",
        )
    return Response(
        content=tif_bytes,
        media_type="image/tiff",
        headers={
            "Content-Disposition": ('attachment; filename="FABDEM_DTM_KML_Clipped.tif"')
        },
    )


@app.post("/api/fabdem-dtm-raw")
async def fabdem_dtm_raw(
    request: Request,
    kml: UploadFile = File(..., description="KML AOI boundary"),
) -> Dict[str, Any]:
    """
    Upload KML → fabdem.download for the KML bbox, clip to exact KML geometry,
    and return a GeoTIFF download URL. No pixel smoothing: native 30 m cells.
    """
    kml_bytes = await kml.read()
    if not kml_bytes:
        raise HTTPException(status_code=400, detail="KML file is empty.")

    try:
        result = await asyncio.to_thread(download_fabdem_dtm_raw_from_kml, kml_bytes)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500, detail=f"FABDEM DTM (no smoothing) failed: {exc}"
        ) from exc

    tif_id = uuid.uuid4().hex
    _TIF_CACHE[tif_id] = result.pop("tif_bytes")
    result["tif_id"] = tif_id
    result["tif_download_url"] = _public_url(
        request, f"/api/fabdem-dtm-raw/tif/{tif_id}"
    )
    return result


@app.get("/api/fabdem-dtm-raw/tif/{tif_id}")
async def download_fabdem_dtm_raw_tif(tif_id: str) -> Response:
    tif_bytes = _TIF_CACHE.get(tif_id)
    if tif_bytes is None:
        raise HTTPException(
            status_code=404,
            detail="TIFF not found or expired. Run POST /api/fabdem-dtm-raw again.",
        )
    return Response(
        content=tif_bytes,
        media_type="image/tiff",
        headers={
            "Content-Disposition": (
                'attachment; filename="FABDEM_DTM_KML_Clipped_NoSmoothing.tif"'
            )
        },
    )


ALL_APIS_MAX_CONCURRENCY = int(os.environ.get("ALL_APIS_MAX_CONCURRENCY", "2"))
ALL_API_NAMES = (
    "flood_water",
    "main_bod_cod",
    "streams_bod_cod",
    "vegetation_type",
    "vegetation_health",
    "lulc",
    "salinity",
    "bank_erosion",
    "water_quality",
    "eutrophication",
    "nitrogen",
    "coliform",
    "water_depth",
    "lithology",
    "silt_classification",
    "silt_volume",
    "fishing_point",
    "fabdem_dtm",
)


@app.post("/api/all")
async def all_apis(
    request: Request,
    kml: UploadFile = File(..., description="One KML AOI used for every API"),
    depth_min_m: Optional[float] = Form(
        None, description="Water-depth minimum depth in metres (optional)"
    ),
    depth_max_m: Optional[float] = Form(
        None, description="Water-depth maximum depth in metres (optional)"
    ),
    apis: Optional[str] = Form(
        None,
        description=(
            "Optional comma-separated subset, e.g. 'salinity,silt_volume'. "
            f"Default: all ({', '.join(ALL_API_NAMES)})"
        ),
    ),
) -> Dict[str, Any]:
    """
    Upload ONE KML → run every JalNetra API and return all responses + KML URLs.

    Each API runs through its own endpoint unchanged; one failing API does not
    stop the others (its entry has status "error"). KML / TIFF download URLs
    in each result are the same as from the individual endpoints.
    """
    kml_bytes = await kml.read()
    if not kml_bytes:
        raise HTTPException(status_code=400, detail="KML file is empty.")
    filename = kml.filename or "aoi.kml"

    def upload() -> UploadFile:
        return UploadFile(file=io.BytesIO(kml_bytes), filename=filename)

    jobs = {
        "flood_water": lambda: flood_water(request, upload()),
        "main_bod_cod": lambda: main_bod_cod(request, upload()),
        "streams_bod_cod": lambda: streams_bod_cod(request, upload()),
        "vegetation_type": lambda: vegetation_type(request, upload()),
        "vegetation_health": lambda: vegetation_health(request, upload()),
        "lulc": lambda: lulc(request, upload()),
        "salinity": lambda: salinity(request, upload()),
        "bank_erosion": lambda: bank_erosion(request, upload()),
        "water_quality": lambda: water_quality(request, upload()),
        "eutrophication": lambda: eutrophication(request, upload()),
        "nitrogen": lambda: nitrogen(request, upload()),
        "coliform": lambda: coliform(request, upload()),
        "water_depth": lambda: water_depth(request, upload(), depth_min_m, depth_max_m),
        "lithology": lambda: lithology(request, upload()),
        "silt_classification": lambda: silt_classification(request, upload()),
        "silt_volume": lambda: silt_volume(request, upload()),
        "fishing_point": lambda: fishing_point(request, upload()),
        "fabdem_dtm": lambda: fabdem_dtm(request, upload()),
    }

    selected = list(ALL_API_NAMES)
    if apis:
        selected = [a.strip() for a in apis.split(",") if a.strip()]
        unknown = [a for a in selected if a not in jobs]
        if unknown:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown api name(s): {unknown}. Valid: {list(ALL_API_NAMES)}",
            )

    semaphore = asyncio.Semaphore(max(1, ALL_APIS_MAX_CONCURRENCY))

    async def _run(name: str) -> Tuple[str, Dict[str, Any]]:
        async with semaphore:
            started = time.perf_counter()
            try:
                data = await jobs[name]()
                entry: Dict[str, Any] = {"status": "ok", "result": data}
            except HTTPException as exc:
                entry = {
                    "status": "error",
                    "status_code": exc.status_code,
                    "detail": exc.detail,
                }
            except Exception as exc:
                entry = {"status": "error", "status_code": 500, "detail": str(exc)}
            entry["elapsed_s"] = round(time.perf_counter() - started, 1)
            return name, entry

    pairs = await asyncio.gather(*(_run(name) for name in selected))
    results = dict(pairs)
    return {
        "kml_filename": filename,
        "api_count": len(selected),
        "succeeded": [n for n in selected if results[n]["status"] == "ok"],
        "failed": [n for n in selected if results[n]["status"] != "ok"],
        "results": results,
    }


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8010))
    lan_host = _network_host()
    print(f"JalNetra API (this PC)  -> http://localhost:{port}/")
    print(f"Docs (this PC)          -> http://localhost:{port}/docs")
    print(f"Network share           -> http://{lan_host}:{port}/")
    print(f"Network docs            -> http://{lan_host}:{port}/docs")
    print("Share the Network URL with others on the same Wi-Fi/LAN.")
    if not os.environ.get("EE_SERVICE_ACCOUNT_JSON"):
        print("[warn] EE_SERVICE_ACCOUNT_JSON not set — flood-water endpoint disabled.")
        print("       Add credentials to .env (see flood_tile setup).")
    try:
        uvicorn.run(
            "api_main:app",
            host="0.0.0.0",
            port=port,
            reload=True,
            # Writing KMLs must not restart the server (that drops in-memory cache).
            reload_excludes=[".kml_cache/*", "**/.kml_cache/**"],
        )
    except OSError as exc:
        if getattr(exc, "winerror", None) == 10013 or "10048" in str(exc):
            print(
                f"\n[error] Port {port} is blocked or already in use.\n"
                f"        Try:  $env:PORT=8010; python api_main.py\n"
                f"        Or:   $env:PORT=8004; python api_main.py"
            )
        raise
