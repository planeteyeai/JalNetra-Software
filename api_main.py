#!/usr/bin/env python3
"""
api_main.py — Unified JalNetra REST API.

Endpoints:
  POST /api/flood-water   — KML + date range → flood/water/area datewise (Sentinel-1)
  POST /api/bod-cod       — KML → BOD/COD live history + 10-day forecast

Usage:
    python api_main.py
    EE_SERVICE_ACCOUNT_JSON='...' python api_main.py   # flood endpoint needs GEE

Flood logic reuses ../flood_tile (Sentinel-1 SAR).
BOD/COD logic reuses jalnetra demo pipeline (synthetic demo data).
Vegetation type/health uses Sentinel-2 + Dynamic World (GEE).
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
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

from jalnetra.api_service import bod_cod_pipeline
from jalnetra.bank_erosion_service import (
    analyze_bank_erosion,
    bank_erosion_geometry,
)
from jalnetra.kml_buffer import buffered_analysis_geometry
from jalnetra.lithology_service import analyze_lithology, lithology_geometry
from jalnetra.lulc_service import analyze_lulc, lulc_analysis_geometry
from jalnetra.salinity_service import analyze_salinity, salinity_geometry
from jalnetra.silt_service import analyze_silt, silt_geometry
from jalnetra.fishing_point_service import (
    analyze_fishing_points,
    fishing_point_geometry,
)
from jalnetra.fabdem_service import download_fabdem_dtm_from_kml
from jalnetra.water_quality_service import (
    analyze_water_quality,
    water_quality_geometry,
)
<<<<<<< HEAD
from jalnetra.water_depth_service import analyze_live_water_depth  # noqa: E402
from jalnetra.vegetation_service import (  # noqa: E402
=======
from jalnetra.vegetation_service import (
>>>>>>> 3e6b88e240eaea9f66df96c63a732b8b498d1cf5
    analyze_vegetation_health,
    analyze_vegetation_type,
    default_date_range,
)
<<<<<<< HEAD
from jalnetra.kml_pixel_smoother import smooth_kml_bytes  # noqa: E402
=======
>>>>>>> 3e6b88e240eaea9f66df96c63a732b8b498d1cf5

_DASHBOARD_CACHE: TTLCache = TTLCache(maxsize=50, ttl=3600)
_EXCEL_CACHE: TTLCache = TTLCache(maxsize=200, ttl=3600)
_KML_CACHE: TTLCache = TTLCache(maxsize=200, ttl=3600)
_TIF_CACHE: TTLCache = TTLCache(maxsize=50, ttl=3600)

RIVER_DASHBOARD_DIRS = {
    "mithi": HERE / "dashboard" / "mithi",
    "mula_mutha": HERE / "dashboard" / "mula_mutha",
    "ganga_pilot": HERE / "dashboard",
    # Any uploaded river (Godavari, etc.) — same HTML shell, unique JSON data
    "custom": HERE / "dashboard" / "mithi",
}


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

# Swagger "Try it out" sends this header so ngrok free tier skips the warning page on API calls.
_NGROK_SWAGGER_INTERCEPTOR = """
(req) => {
  req.headers['ngrok-skip-browser-warning'] = 'true';
  return req;
}
""".strip()


@app.get("/docs", include_in_schema=False)
async def swagger_docs() -> HTMLResponse:
    return get_swagger_ui_html(
        openapi_url=app.openapi_url,
        title=f"{app.title} - Swagger UI",
        swagger_ui_parameters={
            "persistAuthorization": True,
            "requestInterceptor": _NGROK_SWAGGER_INTERCEPTOR,
        },
    )

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _parse_date(label: str, value: str) -> str:
    try:
        return datetime.strptime(value, "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid {label}: use YYYY-MM-DD format.",
        ) from exc


def _geojson_centroid(geojson: Dict[str, Any]) -> Tuple[Optional[float], Optional[float]]:
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


def _embed_dashboard_html(
    river_key: str, data: Dict[str, Any], *, data_url: str
) -> str:
    dash_dir = RIVER_DASHBOARD_DIRS.get(river_key, HERE / "dashboard")
    html_path = dash_dir / "dashboard.html"
    if not html_path.is_file():
        raise FileNotFoundError(f"Dashboard template not found: {html_path}")
    html = html_path.read_text(encoding="utf-8")
    html = html.replace(
        "fetch('dashboard_data.json?t=' + Date.now()",
        f"fetch('{data_url}?t=' + Date.now()",
    )
    marker = '<script id="fallback" type="application/json">'
    end = "</script>"
    i = html.find(marker)
    if i < 0:
        raise ValueError("Dashboard template missing fallback JSON marker.")
    j = html.find(end, i)
    payload = json.dumps(data, separators=(",", ":"))
    return html[: i + len(marker)] + payload + html[j:]


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
            "POST /api/flood-water, POST /api/bod-cod, "
            "POST /api/vegetation-type, POST /api/vegetation-health, "
            "POST /api/lulc, POST /api/salinity, POST /api/bank-erosion, "
<<<<<<< HEAD
            "POST /api/water-quality, POST /api/water-depth, POST /api/lithology, POST /api/silt, "
            "POST /api/fishing-point, POST /api/fabdem-dtm, "
            "POST /api/copernicus-dsm"
=======
            "POST /api/water-quality, POST /api/lithology, POST /api/silt, "
            "POST /api/fishing-point, POST /api/fabdem-dtm"
>>>>>>> 3e6b88e240eaea9f66df96c63a732b8b498d1cf5
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
        _KML_CACHE[kml_id] = entry["kml_bytes"]
        row["kml_id"] = kml_id
        row["kml_download_url"] = _public_url(
            request, f"/api/flood-water/kml/{kml_id}"
        )
        datewise_out.append(row)
    result["datewise"] = datewise_out
    return result


@app.post("/api/flood-water")
async def flood_water(
    request: Request,
    kml: UploadFile = File(..., description="KML file with region boundary"),
    start_date: str = Form(..., description="Range start (YYYY-MM-DD)"),
    end_date: str = Form(..., description="Range end (YYYY-MM-DD)"),
) -> Dict[str, Any]:
    """
    Upload KML + date range → per-image flood/water areas and smoothed KMLs.

    50 m border buffer. Pre/post dates follow Sentinel-1 image sequence.
    No Excel / lat-lon points — class-wise water and flood hectares only.
    """
    _require_earth_engine()

    start_date = _parse_date("start_date", start_date)
    end_date = _parse_date("end_date", end_date)
    if start_date >= end_date:
        raise HTTPException(
            status_code=400,
            detail="start_date must be before end_date.",
        )

    kml_bytes = await kml.read()
    if not kml_bytes:
        raise HTTPException(status_code=400, detail="KML file is empty.")

    try:
        from kml_utils import (  # type: ignore[import-untyped]
            parse_kml_plots,
            plots_to_combined_geometry,
        )
        from jalnetra.flood_water_service import analyze_flood_water_datewise
    except ImportError as exc:
        raise HTTPException(
            status_code=500,
            detail=f"flood_tile modules not found at {FLOOD_TILE_DIR}: {exc}",
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


@app.get("/api/flood-water/kml/{kml_id}")
async def download_flood_water_kml(kml_id: str) -> Response:
    kml_bytes = _KML_CACHE.get(kml_id)
    if kml_bytes is None:
        raise HTTPException(
            status_code=404,
            detail="KML not found or expired. Run POST /api/flood-water again.",
        )
    return Response(
        content=kml_bytes,
        media_type="application/vnd.google-earth.kml+xml",
        headers={"Content-Disposition": 'attachment; filename="flood_water.kml"'},
    )


@app.post("/api/bod-cod")
async def bod_cod(
    request: Request,
    kml: UploadFile = File(..., description="KML river patch"),
) -> Dict[str, Any]:
    """
    Upload KML → BOD/COD for that river (unique per KML).

    River name and bridge / placemark names from the KML drive chainage and
    reach labels. Returns live history, today's snapshot, 10-day forecast,
    and a dashboard_url for an HTML viewer titled for that river.
    """
    kml_bytes = await kml.read()
    if not kml_bytes:
        raise HTTPException(status_code=400, detail="KML file is empty.")

    try:
        result, dashboard_data, river_key = await asyncio.to_thread(
            bod_cod_pipeline, kml_bytes
        )
        viewer_id = uuid.uuid4().hex
        data_url = _public_url(request, f"/api/bod-cod/viewer/{viewer_id}/data")
        html = _embed_dashboard_html(river_key, dashboard_data, data_url=data_url)
        _DASHBOARD_CACHE[viewer_id] = {
            "html": html,
            "data": dashboard_data,
            "river_key": river_key,
        }
        result["viewer_id"] = viewer_id
        result["dashboard_url"] = _public_url(
            request, f"/api/bod-cod/viewer/{viewer_id}"
        )
        result["dashboard_data_url"] = data_url
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"BOD/COD pipeline failed: {exc}",
        ) from exc

    return result


@app.get("/api/bod-cod/viewer/{viewer_id}", response_class=HTMLResponse)
async def bod_cod_viewer(viewer_id: str) -> HTMLResponse:
    """Interactive BOD/COD dashboard HTML for a prior POST /api/bod-cod result."""
    payload = _DASHBOARD_CACHE.get(viewer_id)
    if payload is None:
        raise HTTPException(
            status_code=404,
            detail="Dashboard not found or expired. Run POST /api/bod-cod again.",
        )
    return HTMLResponse(content=payload["html"])


@app.get("/api/bod-cod/viewer/{viewer_id}/data")
async def bod_cod_viewer_data(viewer_id: str) -> Dict[str, Any]:
    """JSON data backing the BOD/COD dashboard viewer."""
    payload = _DASHBOARD_CACHE.get(viewer_id)
    if payload is None:
        raise HTTPException(
            status_code=404,
            detail="Dashboard data not found or expired. Run POST /api/bod-cod again.",
        )
    return payload["data"]


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
    from kml_utils import kml_bytes_to_ee_geometry  # type: ignore[import-untyped]

    return kml_bytes_to_ee_geometry(kml_bytes)


def _vegetation_response(
    request: Request, result: Dict[str, Any], *, prefix: str, filename: str
) -> Dict[str, Any]:
    kml_id = uuid.uuid4().hex
    _KML_CACHE[kml_id] = result.pop("kml_bytes")
    result["kml_id"] = kml_id
    result["kml_download_url"] = _public_url(
        request, f"/api/{prefix}/kml/{kml_id}"
    )
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
        kml_id = uuid.uuid4().hex
        _KML_CACHE[kml_id] = result.pop(bytes_key)
        result[layer_key]["kml_id"] = kml_id
        result[layer_key]["kml_download_url"] = _public_url(
            request, f"/api/water-quality/kml/{kml_id}"
        )
        result[layer_key]["kml_filename"] = filename
    return result


def _silt_response(request: Request, result: Dict[str, Any]) -> Dict[str, Any]:
    """Attach KML download URLs for each month that has imagery."""
    for key in list(result.get("months") or {}):
        bytes_key = f"{key}_kml_bytes"
        if bytes_key not in result:
            continue
        filename = result["months"][key].get("kml_filename", f"silt_{key}.kml")
        kml_id = uuid.uuid4().hex
        _KML_CACHE[kml_id] = result.pop(bytes_key)
        result["months"][key]["kml_id"] = kml_id
        result["months"][key]["kml_download_url"] = _public_url(
            request, f"/api/silt/kml/{kml_id}"
        )
        result["months"][key]["kml_filename"] = filename
    return result


def _water_depth_response(request: Request, result: Dict[str, Any]) -> Dict[str, Any]:
    """Attach permanent-water (blue) and depth KML download URLs."""
    water_id = _store_smoothed_kml(result.pop("water_kml_bytes"))
    depth_id = _store_smoothed_kml(result.pop("depth_kml_bytes"))
    result["permanent_water"] = {
        "kml_id": water_id,
        "kml_download_url": _public_url(
            request, f"/api/water-depth/kml/{water_id}"
        ),
        "kml_filename": result.pop("water_kml_filename", "permanent_water.kml"),
        "color": "#0000FF",
    }
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
        kml_id = uuid.uuid4().hex
        _KML_CACHE[kml_id] = result.pop(bytes_key)
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
    start_date: Optional[str] = Form(
        None, description="Start date YYYY-MM-DD (default: ~31 days ago)"
    ),
    end_date: Optional[str] = Form(
        None, description="End date YYYY-MM-DD (default: today)"
    ),
) -> Dict[str, Any]:
    """
    Upload KML → vegetation type map inside a rectangle (KML bounds + 2 km buffer).

    Returns area (ha) and % of analysis area per class, plus downloadable KML overlay.
    """
    _require_earth_engine()

    if not start_date or not end_date:
        default_start, default_end = default_date_range()
        start_date = start_date or default_start
        end_date = end_date or default_end
    start_date = _parse_date("start_date", start_date)
    end_date = _parse_date("end_date", end_date)
    if start_date >= end_date:
        raise HTTPException(status_code=400, detail="start_date must be before end_date.")

    kml_bytes = await kml.read()
    if not kml_bytes:
        raise HTTPException(status_code=400, detail="KML file is empty.")

    try:
        analysis_geom, _input_geom, buffer_info = await asyncio.to_thread(
            _vegetation_geometry, kml_bytes
        )
        result = await asyncio.to_thread(
            analyze_vegetation_type,
            analysis_geom,
            start_date,
            end_date,
            buffer_info=buffer_info,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500, detail=f"Vegetation type analysis failed: {exc}"
        ) from exc

    return _vegetation_response(
        request, result, prefix="vegetation-type", filename="vegetation_type.kml"
    )


@app.get("/api/vegetation-type/kml/{kml_id}")
async def download_vegetation_type_kml(kml_id: str) -> Response:
    kml_bytes = _KML_CACHE.get(kml_id)
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
    start_date: Optional[str] = Form(
        None, description="Start date YYYY-MM-DD (default: ~31 days ago)"
    ),
    end_date: Optional[str] = Form(
        None, description="End date YYYY-MM-DD (default: today)"
    ),
) -> Dict[str, Any]:
    """
    Upload KML → vegetation health map inside a rectangle (KML bounds + 2 km buffer).

    Returns area (ha) and % of total vegetation per health class, plus downloadable KML.
    """
    _require_earth_engine()

    if not start_date or not end_date:
        default_start, default_end = default_date_range()
        start_date = start_date or default_start
        end_date = end_date or default_end
    start_date = _parse_date("start_date", start_date)
    end_date = _parse_date("end_date", end_date)
    if start_date >= end_date:
        raise HTTPException(status_code=400, detail="start_date must be before end_date.")

    kml_bytes = await kml.read()
    if not kml_bytes:
        raise HTTPException(status_code=400, detail="KML file is empty.")

    try:
        analysis_geom, _input_geom, buffer_info = await asyncio.to_thread(
            _vegetation_geometry, kml_bytes
        )
        result = await asyncio.to_thread(
            analyze_vegetation_health,
            analysis_geom,
            start_date,
            end_date,
            buffer_info=buffer_info,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500, detail=f"Vegetation health analysis failed: {exc}"
        ) from exc

    return _vegetation_response(
        request, result, prefix="vegetation-health", filename="vegetation_health.kml"
    )


@app.get("/api/vegetation-health/kml/{kml_id}")
async def download_vegetation_health_kml(kml_id: str) -> Response:
    kml_bytes = _KML_CACHE.get(kml_id)
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

    return _lulc_response(request, result)


@app.get("/api/lulc/kml/{kml_id}")
async def download_lulc_kml(kml_id: str) -> Response:
    kml_bytes = _KML_CACHE.get(kml_id)
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
    start_date: str = Form(..., description="Start date YYYY-MM-DD"),
    end_date: str = Form(..., description="End date YYYY-MM-DD"),
) -> Dict[str, Any]:
    """
    Upload KML + date range → relative salinity index (0–1) over water pixels.

    Uses latest Sentinel-2 + Dynamic World in the date window.
    Returns class-wise salinity range status (JSON) and downloadable KML overlay.
    """
    _require_earth_engine()

    start_date = _parse_date("start_date", start_date)
    end_date = _parse_date("end_date", end_date)
    if start_date >= end_date:
        raise HTTPException(status_code=400, detail="start_date must be before end_date.")

    kml_bytes = await kml.read()
    if not kml_bytes:
        raise HTTPException(status_code=400, detail="KML file is empty.")

    try:
        aoi_geom, aoi_info = await asyncio.to_thread(salinity_geometry, kml_bytes)
        result = await asyncio.to_thread(
            analyze_salinity,
            aoi_geom,
            start_date,
            end_date,
            aoi_info=aoi_info,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500, detail=f"Salinity analysis failed: {exc}"
        ) from exc

    return _vegetation_response(
        request, result, prefix="salinity", filename="relative_salinity.kml"
    )


@app.get("/api/salinity/kml/{kml_id}")
async def download_salinity_kml(kml_id: str) -> Response:
    kml_bytes = _KML_CACHE.get(kml_id)
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

    return _vegetation_response(
        request,
        result,
        prefix="bank-erosion",
        filename="bank_erosion_hotspots_2016_2026.kml",
    )


@app.get("/api/bank-erosion/kml/{kml_id}")
async def download_bank_erosion_kml(kml_id: str) -> Response:
    kml_bytes = _KML_CACHE.get(kml_id)
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
    start_date: str = Form(..., description="Start date YYYY-MM-DD"),
    end_date: str = Form(..., description="End date YYYY-MM-DD"),
) -> Dict[str, Any]:
    """
    Upload KML + date range → four water quality KML layers.

    Returns WST, TSS (turbidity), NDWI (permanent water), and NDCI (chlorophyll).
    No buffer. Uses latest Sentinel-2 + Dynamic World; Landsat 9 for WST.
    """
    _require_earth_engine()

    start_date = _parse_date("start_date", start_date)
    end_date = _parse_date("end_date", end_date)
    if start_date >= end_date:
        raise HTTPException(status_code=400, detail="start_date must be before end_date.")

    kml_bytes = await kml.read()
    if not kml_bytes:
        raise HTTPException(status_code=400, detail="KML file is empty.")

    try:
        aoi_geom, aoi_info = await asyncio.to_thread(water_quality_geometry, kml_bytes)
        result = await asyncio.to_thread(
            analyze_water_quality,
            aoi_geom,
            start_date,
            end_date,
            aoi_info=aoi_info,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500, detail=f"Water quality analysis failed: {exc}"
        ) from exc

    return _water_quality_response(request, result)


@app.get("/api/water-quality/kml/{kml_id}")
async def download_water_quality_kml(kml_id: str) -> Response:
    kml_bytes = _KML_CACHE.get(kml_id)
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
<<<<<<< HEAD


@app.post("/api/water-depth")
async def water_depth(
    request: Request,
    kml: UploadFile = File(..., description="KML AOI boundary"),
) -> Dict[str, Any]:
    """
    Upload KML → two downloadable KMLs: blue permanent/SAR water and relative depth.

    Response includes the actual Sentinel-1 and Sentinel-2 image dates used.
    """
    _require_earth_engine()

    kml_bytes = await kml.read()
    if not kml_bytes:
        raise HTTPException(status_code=400, detail="KML file is empty.")

    try:
        aoi_geom = await asyncio.to_thread(_kml_geometry_from_bytes, kml_bytes)
        result = await asyncio.to_thread(analyze_live_water_depth, aoi_geom)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500, detail=f"Water depth analysis failed: {exc}"
        ) from exc

    return _water_depth_response(request, result)


@app.get("/api/water-depth/kml/{kml_id}")
async def download_water_depth_kml(kml_id: str) -> Response:
    kml_bytes = _KML_CACHE.get(kml_id)
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
=======
>>>>>>> 3e6b88e240eaea9f66df96c63a732b8b498d1cf5


@app.post("/api/lithology")
async def lithology(
    request: Request,
    kml: UploadFile = File(..., description="KML AOI boundary"),
    start_date: str = Form(..., description="Start date YYYY-MM-DD"),
    end_date: str = Form(..., description="End date YYYY-MM-DD"),
) -> Dict[str, Any]:
    """
    Upload KML + date range → lithological spectral interpretation map.

    Analysis uses a 50 m buffer from the KML border.
    Sentinel-2 dry-season median, K-Means clustering, silt class 0 and
    eight lithology spectral clusters (1–8). Returns class areas (ha) and
    percent of classified area, plus downloadable smoothed KML overlay.
    """
    _require_earth_engine()

    start_date = _parse_date("start_date", start_date)
    end_date = _parse_date("end_date", end_date)
    if start_date >= end_date:
        raise HTTPException(status_code=400, detail="start_date must be before end_date.")

    kml_bytes = await kml.read()
    if not kml_bytes:
        raise HTTPException(status_code=400, detail="KML file is empty.")

    try:
        analysis_geom, _input_geom, buffer_info = await asyncio.to_thread(
            lithology_geometry, kml_bytes
        )
        result = await asyncio.to_thread(
            analyze_lithology,
            analysis_geom,
            start_date,
            end_date,
            buffer_info=buffer_info,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500, detail=f"Lithology analysis failed: {exc}"
        ) from exc

    return _vegetation_response(
        request, result, prefix="lithology", filename="lithology.kml"
    )


@app.get("/api/lithology/kml/{kml_id}")
async def download_lithology_kml(kml_id: str) -> Response:
    kml_bytes = _KML_CACHE.get(kml_id)
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


@app.post("/api/silt")
async def silt(
    request: Request,
    kml: UploadFile = File(..., description="KML AOI boundary (no buffer)"),
) -> Dict[str, Any]:
    """
    Upload KML → monthly silt classification from January 2026 through today.

    No buffer. Months without Sentinel-2 imagery are skipped. Returns one
    smoothed KML per available month plus class areas as % of water area.
    Dynamic World is not used.
    """
    _require_earth_engine()

    kml_bytes = await kml.read()
    if not kml_bytes:
        raise HTTPException(status_code=400, detail="KML file is empty.")

    try:
        aoi_geom, aoi_info = await asyncio.to_thread(silt_geometry, kml_bytes)
        result = await asyncio.to_thread(
            analyze_silt,
            aoi_geom,
            aoi_info=aoi_info,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500, detail=f"Silt analysis failed: {exc}"
        ) from exc

    return _silt_response(request, result)


@app.get("/api/silt/kml/{kml_id}")
async def download_silt_kml(kml_id: str) -> Response:
    kml_bytes = _KML_CACHE.get(kml_id)
    if kml_bytes is None:
        raise HTTPException(
            status_code=404,
            detail="KML not found or expired. Run POST /api/silt again.",
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
    _KML_CACHE[kml_id] = result.pop("kml_bytes")
    result["kml_id"] = kml_id
    result["kml_download_url"] = _public_url(
        request, f"/api/fishing-point/kml/{kml_id}"
    )
    result["kml_filename"] = result.get("kml_filename", "fishing_points.kml")
    return result


@app.get("/api/fishing-point/kml/{kml_id}")
async def download_fishing_point_kml(kml_id: str) -> Response:
    kml_bytes = _KML_CACHE.get(kml_id)
    if kml_bytes is None:
        raise HTTPException(
            status_code=404,
            detail="KML not found or expired. Run POST /api/fishing-point again.",
        )
    return Response(
        content=kml_bytes,
        media_type="application/vnd.google-earth.kml+xml",
        headers={
            "Content-Disposition": 'attachment; filename="fishing_points.kml"'
        },
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
    result["tif_download_url"] = _public_url(
        request, f"/api/fabdem-dtm/tif/{tif_id}"
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
            "Content-Disposition": (
                'attachment; filename="FABDEM_DTM_KML_Clipped.tif"'
            )
        },
    )


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
        )
    except OSError as exc:
        if getattr(exc, "winerror", None) == 10013 or "10048" in str(exc):
            print(
                f"\n[error] Port {port} is blocked or already in use.\n"
                f"        Try:  $env:PORT=8010; python api_main.py\n"
                f"        Or:   $env:PORT=8004; python api_main.py"
            )
        raise
