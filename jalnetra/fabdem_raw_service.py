"""
jalnetra.fabdem_raw_service — FABDEM DTM clipped to the uploaded KML, with no
pixel smoothing (native 30 m cells).

Port of the standalone FABDEM KML DTM downloader script.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any, Dict

import geopandas as gpd
import rasterio
from rasterio.mask import mask

from jalnetra.fabdem_service import _download_fabdem_for_bounds


def download_fabdem_dtm_raw_from_kml(kml_bytes: bytes) -> Dict[str, Any]:
    if not kml_bytes:
        raise ValueError("KML file is empty.")

    project_root = Path(__file__).resolve().parent.parent
    cache_dir = project_root / "FABDEM_Cache"
    cache_dir.mkdir(exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="fabdem_raw_") as tmp:
        tmp_dir = Path(tmp)
        kml_file = tmp_dir / "My_AOI.kml"
        kml_file.write_bytes(kml_bytes)

        output_dir = tmp_dir / "FABDEM_Output"
        output_dir.mkdir(exist_ok=True)

        temp_file = output_dir / "FABDEM_Temporary.tif"
        output_file = output_dir / "FABDEM_DTM_KML_Clipped.tif"

        # ---- read KML ----
        try:
            aoi = gpd.read_file(kml_file, driver="KML")
        except Exception as error:
            raise ValueError(f"Failed to read KML: {error}") from error

        # ---- check geometry ----
        if aoi.empty:
            raise ValueError("KML contains no geometry.")

        aoi = aoi[aoi.geometry.notnull()]
        if aoi.empty:
            raise ValueError("No valid geometry found in KML.")

        # ---- CRS ----
        if aoi.crs is None:
            aoi = aoi.set_crs("EPSG:4326")
        else:
            aoi = aoi.to_crs("EPSG:4326")

        # ---- merge geometries + bbox ----
        aoi_geometry = aoi.geometry.union_all()
        west, south, east, north = aoi_geometry.bounds

        # ---- FABDEM download (HTTP Range, only the needed tiles) ----
        try:
            bounds = (float(west), float(south), float(east), float(north))
            dl_meta = _download_fabdem_for_bounds(bounds, temp_file, cache_dir)
        except Exception as error:
            raise RuntimeError(f"FABDEM download failed: {error}") from error

        # ---- clip FABDEM using KML ----
        try:
            with rasterio.open(temp_file) as src:
                geometry = [aoi_geometry.__geo_interface__]
                clipped, clipped_transform = mask(src, geometry, crop=True)

                output_meta = src.meta.copy()
                output_meta.update(
                    {
                        "driver": "GTiff",
                        "height": clipped.shape[1],
                        "width": clipped.shape[2],
                        "transform": clipped_transform,
                    }
                )

                with rasterio.open(output_file, "w", **output_meta) as dst:
                    dst.write(clipped)
        except Exception as error:
            raise RuntimeError(f"Clipping failed: {error}") from error

        tif_bytes = output_file.read_bytes()

    return {
        "bounds": {
            "west": float(west),
            "south": float(south),
            "east": float(east),
            "north": float(north),
        },
        "geometry_type": str(aoi_geometry.geom_type),
        "crs": "EPSG:4326",
        "width": int(clipped.shape[2]),
        "height": int(clipped.shape[1]),
        "tif_filename": "FABDEM_DTM_KML_Clipped.tif",
        "tif_bytes": tif_bytes,
        "smoothing": None,
        "download": dl_meta,
        "notes": {
            "source": "FABDEM (Forest And Buildings removed Copernicus DEM)",
            "clip": "Clipped to exact uploaded KML geometry (not only bbox).",
            "method": (
                "HTTP Range extraction of only the needed tiles for the KML bbox, "
                "then rasterio mask; no smoothing."
            ),
        },
    }
