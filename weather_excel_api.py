"""
Standalone Open-Meteo weather Excel API.

Accepts an Excel file + start_date + end_date; returns a new Excel with
Temperature and Rainfall for each row's lat/lon. Does not modify any
existing JalNetra modules.

- Temperature: average of daily mean temps over the date range
- Rainfall: total of daily rainfall over the date range
- Same start and end date => that single day's values

Run:
    uvicorn weather_excel_api:app --host 0.0.0.0 --port 8090 --reload
"""

from __future__ import annotations

import io
import math
import time
from datetime import date, datetime, timedelta

import pandas as pd
import requests
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import StreamingResponse

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
COORD_DECIMALS = 2
REQUEST_PAUSE_SEC = 0.12

app = FastAPI(
    title="Weather Excel API",
    description=(
        "Fill Temperature and Rainfall into an Excel file for a date range "
        "using Open-Meteo (temp = average, rainfall = total)."
    ),
    version="1.1.0",
)


def _find_column(columns: list[str], *candidates: str) -> str | None:
    lower_map = {str(c).lower().strip(): c for c in columns}
    for name in candidates:
        if name.lower() in lower_map:
            return lower_map[name.lower()]
    for name in candidates:
        key = name.lower()
        for col_l, col in lower_map.items():
            if key in col_l:
                return col
    return None


def _parse_date(value: str, field_name: str) -> str:
    value = (value or "").strip()
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(value, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    raise HTTPException(
        status_code=400,
        detail=f"Invalid {field_name}. Use YYYY-MM-DD (example: 2026-06-20).",
    )


def _pick_api_url(start_date: str, end_date: str) -> str:
    """Use archive when the whole range is older; else forecast."""
    end_d = datetime.strptime(end_date, "%Y-%m-%d").date()
    if end_d <= date.today() - timedelta(days=5):
        return ARCHIVE_URL
    return FORECAST_URL


def fetch_weather(
    lat: float, lon: float, start_date: str, end_date: str
) -> tuple[float | None, float | None]:
    """Return (avg temperature °C, total rainfall mm) for the date range."""
    if any(math.isnan(x) for x in (lat, lon)):
        return None, None

    params = {
        "latitude": lat,
        "longitude": lon,
        "start_date": start_date,
        "end_date": end_date,
        "daily": "temperature_2m_mean,precipitation_sum",
        "timezone": "auto",
    }
    url = _pick_api_url(start_date, end_date)
    resp = requests.get(url, params=params, timeout=60)
    resp.raise_for_status()
    daily = resp.json().get("daily") or {}
    temps = [t for t in (daily.get("temperature_2m_mean") or []) if t is not None]
    rains = [r for r in (daily.get("precipitation_sum") or []) if r is not None]

    mean_temp = round(sum(temps) / len(temps), 2) if temps else None
    total_rain = round(sum(rains), 2) if rains else None
    return mean_temp, total_rain


def enrich_excel_with_weather(
    df: pd.DataFrame, start_date: str, end_date: str
) -> pd.DataFrame:
    from_lat_col = _find_column(list(df.columns), "From Latitude", "Latitude", "lat")
    from_lon_col = _find_column(list(df.columns), "From Longitude", "Longitude", "lon", "lng")
    to_lat_col = _find_column(list(df.columns), "To Latitude")
    to_lon_col = _find_column(list(df.columns), "To Longitude")

    if not from_lat_col or not from_lon_col:
        raise HTTPException(
            status_code=400,
            detail="Excel must contain latitude/longitude columns "
            "(e.g. 'From Latitude' / 'From Longitude' or 'Latitude' / 'Longitude').",
        )

    temp_col = next((c for c in df.columns if "temperature" in str(c).lower()), None)
    rain_col = next(
        (
            c
            for c in df.columns
            if "rainfall" in str(c).lower() or "precipitation" in str(c).lower()
        ),
        None,
    )
    if temp_col is None:
        temp_col = "Temperature (°C):"
        df[temp_col] = pd.NA
    if rain_col is None:
        rain_col = "Rainfall (mm):"
        df[rain_col] = pd.NA

    from_lat = pd.to_numeric(df[from_lat_col], errors="coerce")
    from_lon = pd.to_numeric(df[from_lon_col], errors="coerce")
    mid_lat = from_lat.copy()
    mid_lon = from_lon.copy()

    if to_lat_col and to_lon_col:
        to_lat = pd.to_numeric(df[to_lat_col], errors="coerce")
        to_lon = pd.to_numeric(df[to_lon_col], errors="coerce")
        both_ok = to_lat.notna() & to_lon.notna()
        mid_lat = mid_lat.where(~both_ok, (from_lat + to_lat) / 2.0)
        mid_lon = mid_lon.where(~both_ok, (from_lon + to_lon) / 2.0)

    df = df.copy()
    df["_lat_key"] = mid_lat.round(COORD_DECIMALS)
    df["_lon_key"] = mid_lon.round(COORD_DECIMALS)

    keys = df[["_lat_key", "_lon_key"]].drop_duplicates()
    cache: dict[tuple[float, float], tuple[float | None, float | None]] = {}

    for row in keys.to_dict("records"):
        lat_k = row["_lat_key"]
        lon_k = row["_lon_key"]
        if pd.isna(lat_k) or pd.isna(lon_k):
            continue
        key = (float(lat_k), float(lon_k))
        try:
            cache[key] = fetch_weather(key[0], key[1], start_date, end_date)
        except Exception:
            cache[key] = (None, None)
        time.sleep(REQUEST_PAUSE_SEC)

    temps: list[float | None] = []
    rains: list[float | None] = []
    for row in df.to_dict("records"):
        lat_k = row["_lat_key"]
        lon_k = row["_lon_key"]
        if pd.isna(lat_k) or pd.isna(lon_k):
            temps.append(None)
            rains.append(None)
            continue
        t, r = cache.get((float(lat_k), float(lon_k)), (None, None))
        temps.append(t)
        rains.append(r)

    period_label = start_date if start_date == end_date else f"{start_date} to {end_date}"

    df[temp_col] = temps
    df[rain_col] = rains
    df["OpenMeteo_Temp_C"] = temps
    df["OpenMeteo_Rainfall_mm"] = rains
    df["OpenMeteo_Lat_Used"] = df["_lat_key"]
    df["OpenMeteo_Lon_Used"] = df["_lon_key"]
    df["OpenMeteo_Start_Date"] = start_date
    df["OpenMeteo_End_Date"] = end_date
    df["OpenMeteo_Period"] = period_label
    return df.drop(columns=["_lat_key", "_lon_key"])


@app.get("/")
def root():
    return {
        "service": "Weather Excel API",
        "endpoint": "POST /weather/excel",
        "inputs": {
            "file": "Excel (.xlsx)",
            "start_date": "YYYY-MM-DD",
            "end_date": "YYYY-MM-DD",
        },
        "logic": {
            "temperature": "average of daily mean temps in range",
            "rainfall": "total of daily rainfall in range",
        },
        "output": "Excel with Temperature and Rainfall filled via Open-Meteo",
    }


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/weather/excel")
async def weather_excel(
    file: UploadFile = File(..., description="Excel file with lat/lon columns"),
    start_date: str = Form(..., description="Start date YYYY-MM-DD"),
    end_date: str = Form(..., description="End date YYYY-MM-DD"),
):
    start = _parse_date(start_date, "start_date")
    end = _parse_date(end_date, "end_date")

    if datetime.strptime(start, "%Y-%m-%d") > datetime.strptime(end, "%Y-%m-%d"):
        raise HTTPException(status_code=400, detail="start_date must be on or before end_date.")

    filename = (file.filename or "").lower()
    if not filename.endswith((".xlsx", ".xls")):
        raise HTTPException(status_code=400, detail="Upload an Excel file (.xlsx or .xls).")

    try:
        content = await file.read()
        df = pd.read_excel(io.BytesIO(content))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Could not read Excel: {exc}") from exc

    if df.empty:
        raise HTTPException(status_code=400, detail="Excel file has no rows.")

    try:
        out_df = enrich_excel_with_weather(df, start, end)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Weather fill failed: {exc}") from exc

    buf = io.BytesIO()
    out_df.to_excel(buf, index=False)
    buf.seek(0)

    base = (file.filename or "input.xlsx").rsplit(".", 1)[0]
    out_name = f"{base}_weather_{start}_to_{end}.xlsx"
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{out_name}"'},
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("weather_excel_api:app", host="0.0.0.0", port=8090, reload=False)
