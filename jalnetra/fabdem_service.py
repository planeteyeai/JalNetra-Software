"""
jalnetra.fabdem_service — FABDEM DTM download clipped to uploaded KML.

Port of the FABDEM KML DTM downloader script:
  1) read KML → WGS84
  2) download FABDEM for KML bounding box
  3) clip raster to exact KML geometry
  4) return clipped GeoTIFF bytes

Production note:
  The public ``fabdem`` package falls back to downloading whole ZIP archives
  (often 1–2 GB). On Railway that commonly yields a truncated / HTML body and
  then ``BadZipFile: File is not a zip file``. This module downloads only the
  required TIFF members via HTTP Range requests and validates ZIP/TIFF magic.
"""
from __future__ import annotations

import logging
import struct
import tempfile
import zlib
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import geopandas as gpd
import numpy as np
import rasterio
import rasterio.merge
import requests
import shapely.geometry
from rasterio.enums import Resampling
from rasterio.features import geometry_mask
from rasterio.mask import mask
from rasterio.transform import Affine
from rasterio.warp import reproject
from requests import Session
from scipy.ndimage import gaussian_filter

logger = logging.getLogger(__name__)

FABDEM_BASE_URL = "https://data.bris.ac.uk/datasets/s5hqmjcdj8yo2ibzi9b4ew3sn"
TILES_GEOJSON = f"{FABDEM_BASE_URL}/FABDEM_v1-2_tiles.geojson"
USER_AGENT = (
    "JalNetra-FABDEM/1.0 (+https://github.com/planeteyeai/JalNetra-Software; "
    "mailto:support@planeteye.ai)"
)
REQUEST_TIMEOUT = (30, 600)  # connect, read
MAX_RETRIES = 3

# DTM edge smoothing: bilinear upsample then nodata-aware Gaussian so 30 m
# FABDEM cells render as continuous slopes instead of square steps.
DTM_SMOOTH_UPSCALE = 3
DTM_SMOOTH_SIGMA_PX = 1.5  # in upsampled pixels
DTM_SMOOTH_MAX_PIXELS = 40_000_000
DTM_NODATA = -9999.0


def _session() -> Session:
    s = Session()
    s.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept": "*/*",
        }
    )
    return s


def _purge_invalid_cache(cache_dir: Path) -> List[str]:
    """Remove non-ZIP / truncated archives that poison subsequent runs."""
    removed: List[str] = []
    if not cache_dir.exists():
        return removed
    for path in cache_dir.glob("*.zip"):
        try:
            with open(path, "rb") as fh:
                magic = fh.read(4)
            if magic[:2] != b"PK":
                path.unlink(missing_ok=True)
                removed.append(path.name)
                continue
            # Tiny files cannot be real FABDEM zips (multi-hundred MB+)
            if path.stat().st_size < 1024 * 1024:
                path.unlink(missing_ok=True)
                removed.append(path.name)
        except OSError:
            continue
    return removed


def _correct_tile_name(json_name: str) -> str:
    # FABDEM_v1-2_tiles.geojson north/south labels have an extra zero.
    return json_name[0] + json_name[2:]


def _normalize_zip_name(name: str) -> str:
    return name.replace("S-", "S").replace("N-", "N")


def _head_file(session: Session, url: str) -> Tuple[int, bool]:
    response = session.head(url, allow_redirects=True, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    file_size = int(response.headers.get("content-length", 0))
    accept_ranges = response.headers.get("accept-ranges", "")
    return file_size, "bytes" in accept_ranges.lower()


def _download_byte_range(
    session: Session, url: str, start: int, end: int
) -> bytes:
    last_exc: Optional[BaseException] = None
    for attempt in range(MAX_RETRIES):
        try:
            response = session.get(
                url,
                headers={"Range": f"bytes={start}-{end}"},
                stream=True,
                timeout=REQUEST_TIMEOUT,
            )
            response.raise_for_status()
            if response.status_code != 206:
                raise ValueError(
                    f"Server did not return HTTP 206 for range {start}-{end} "
                    f"(got {response.status_code})."
                )
            chunks: List[bytes] = []
            for chunk in response.iter_content(chunk_size=1024 * 64):
                if chunk:
                    chunks.append(chunk)
            data = b"".join(chunks)
            expected = end - start + 1
            if len(data) != expected:
                raise ValueError(
                    f"Incomplete range download ({len(data)}/{expected} bytes)."
                )
            return data
        except Exception as exc:
            last_exc = exc
            logger.warning(
                "FABDEM range download attempt %d/%d failed: %s",
                attempt + 1,
                MAX_RETRIES,
                exc,
            )
    assert last_exc is not None
    raise last_exc


def _parse_zip64_extra(
    extra: bytes,
    *,
    uncompressed_size: int,
    compressed_size: int,
    local_header_offset: int,
    disk_start: int = 0,
) -> Tuple[int, int, int, int]:
    """
    Resolve 0xFFFFFFFF / 0xFFFF placeholders from ZIP64 extra field (0x0001).

    Large FABDEM archives (e.g. N20E090…) store real local-header offsets in
    ZIP64 extras; treating 0xFFFFFFFF as a real offset causes HTTP 416.
    """
    pos = 0
    while pos + 4 <= len(extra):
        header_id, data_size = struct.unpack_from("<HH", extra, pos)
        pos += 4
        if pos + data_size > len(extra):
            break
        data = extra[pos : pos + data_size]
        pos += data_size
        if header_id != 0x0001:
            continue
        o = 0
        if uncompressed_size == 0xFFFFFFFF:
            if o + 8 > len(data):
                raise ValueError("Truncated ZIP64 uncompressed size.")
            uncompressed_size = struct.unpack_from("<Q", data, o)[0]
            o += 8
        if compressed_size == 0xFFFFFFFF:
            if o + 8 > len(data):
                raise ValueError("Truncated ZIP64 compressed size.")
            compressed_size = struct.unpack_from("<Q", data, o)[0]
            o += 8
        if local_header_offset == 0xFFFFFFFF:
            if o + 8 > len(data):
                raise ValueError("Truncated ZIP64 local header offset.")
            local_header_offset = struct.unpack_from("<Q", data, o)[0]
            o += 8
        if disk_start == 0xFFFF:
            if o + 4 > len(data):
                raise ValueError("Truncated ZIP64 disk start.")
            disk_start = struct.unpack_from("<I", data, o)[0]
        break
    return uncompressed_size, compressed_size, local_header_offset, disk_start


def _read_eocd_offsets(tail_bytes: bytes) -> Tuple[int, int, int]:
    """Return (central_dir_offset, central_dir_size, total_entries) from ZIP tail."""
    # Prefer ZIP64 EOCD locator when present (PK\\x06\\x07)
    zip64_loc = tail_bytes.rfind(b"PK\x06\x07")
    if zip64_loc != -1 and zip64_loc + 20 <= len(tail_bytes):
        _sig, _disk, zip64_eocd_offset, _disks = struct.unpack_from(
            "<4sIQI", tail_bytes, zip64_loc
        )
        # Locator alone is not enough if ZIP64 EOCD is outside this tail; fall
        # through to classic EOCD and let callers fetch a larger window if needed.
        _ = zip64_eocd_offset

    signature = b"PK\x05\x06"
    index = tail_bytes.rfind(signature)
    if index == -1:
        raise ValueError("ZIP end of central directory record not found.")

    eocd = tail_bytes[index : index + 22]
    (
        _sig,
        disk_number,
        central_dir_disk_number,
        disk_entries,
        total_entries,
        central_dir_size,
        central_dir_offset,
        _comment_length,
    ) = struct.unpack("<4s4H2LH", eocd)

    if disk_number != 0 or central_dir_disk_number != 0:
        raise ValueError("Multi-disk ZIP archives are not supported.")
    if disk_entries != total_entries and total_entries != 0xFFFF:
        raise ValueError("ZIP archive spans multiple disks and is not supported.")

    # ZIP64 EOCD record (PK\\x06\\x06) when classic fields are overflowed
    if (
        central_dir_offset == 0xFFFFFFFF
        or central_dir_size == 0xFFFFFFFF
        or total_entries == 0xFFFF
    ):
        zip64_eocd = tail_bytes.rfind(b"PK\x06\x06")
        if zip64_eocd == -1:
            raise ValueError(
                "ZIP64 end of central directory required but not found in file tail."
            )
        # After sig(4) + size(8) + version made(2) + version needed(2):
        # disk(4), cd_disk(4), disk_entries(8), total_entries(8), cd_size(8), cd_offset(8)
        (
            _zsig,
            _zsize,
            _vmade,
            _vneed,
            _zdisk,
            _zcd_disk,
            _zdisk_entries,
            total_entries64,
            central_dir_size64,
            central_dir_offset64,
        ) = struct.unpack_from("<4sQHHIIQQQQ", tail_bytes, zip64_eocd)
        if central_dir_offset == 0xFFFFFFFF:
            central_dir_offset = central_dir_offset64
        if central_dir_size == 0xFFFFFFFF:
            central_dir_size = central_dir_size64
        if total_entries == 0xFFFF:
            total_entries = total_entries64

    return int(central_dir_offset), int(central_dir_size), int(total_entries)


def _read_remote_zip_index(session: Session, url: str) -> Dict[str, Dict[str, int]]:
    file_size, supports_ranges = _head_file(session, url)
    if not file_size:
        raise ValueError("Could not determine remote ZIP file size.")
    if not supports_ranges:
        raise ValueError(
            "FABDEM host does not support HTTP Range requests; "
            "cannot safely download multi-GB archives on Railway."
        )

    # Larger tail so ZIP64 EOCD + classic EOCD both fit for multi-GB archives
    tail_size = min(file_size, 256 * 1024)
    tail_start = file_size - tail_size
    tail_bytes = _download_byte_range(session, url, tail_start, file_size - 1)

    central_dir_offset, central_dir_size, _total_entries = _read_eocd_offsets(
        tail_bytes
    )
    if central_dir_offset < 0 or central_dir_size <= 0:
        raise ValueError("Invalid ZIP central directory offsets.")
    if central_dir_offset + central_dir_size > file_size:
        raise ValueError(
            f"ZIP central directory exceeds file size "
            f"(offset={central_dir_offset}, size={central_dir_size}, file={file_size})."
        )

    central_directory = _download_byte_range(
        session,
        url,
        central_dir_offset,
        central_dir_offset + central_dir_size - 1,
    )

    entries: Dict[str, Dict[str, int]] = {}
    offset = 0
    while offset < len(central_directory):
        if central_directory[offset : offset + 4] != b"PK\x01\x02":
            raise ValueError("Invalid ZIP central directory entry.")
        header = central_directory[offset : offset + 46]
        (
            _signature,
            _version_made_by,
            _version_needed,
            flags,
            compression_method,
            _mod_time,
            _mod_date,
            crc32,
            compressed_size,
            uncompressed_size,
            filename_length,
            extra_length,
            comment_length,
            disk_start,
            _internal_attributes,
            _external_attributes,
            local_header_offset,
        ) = struct.unpack("<4s6H3L5H2L", header)

        filename_start = offset + 46
        filename_end = filename_start + filename_length
        extra_end = filename_end + extra_length
        comment_end = extra_end + comment_length
        filename = central_directory[filename_start:filename_end].decode("utf-8")
        extra = central_directory[filename_end:extra_end]

        if (
            uncompressed_size == 0xFFFFFFFF
            or compressed_size == 0xFFFFFFFF
            or local_header_offset == 0xFFFFFFFF
            or disk_start == 0xFFFF
        ):
            (
                uncompressed_size,
                compressed_size,
                local_header_offset,
                disk_start,
            ) = _parse_zip64_extra(
                extra,
                uncompressed_size=uncompressed_size,
                compressed_size=compressed_size,
                local_header_offset=local_header_offset,
                disk_start=disk_start,
            )

        if local_header_offset == 0xFFFFFFFF or local_header_offset >= file_size:
            raise ValueError(
                f"Invalid local header offset for {filename}: {local_header_offset} "
                f"(file size {file_size}). ZIP64 extra field missing or corrupt."
            )
        if compressed_size <= 0:
            raise ValueError(f"Invalid compressed size for {filename}: {compressed_size}")

        entries[filename] = {
            "flags": flags,
            "compression_method": compression_method,
            "crc32": crc32,
            "compressed_size": int(compressed_size),
            "uncompressed_size": int(uncompressed_size),
            "local_header_offset": int(local_header_offset),
        }
        offset = comment_end

    return entries


def _extract_remote_zip_member(
    session: Session,
    url: str,
    entries: Dict[str, Dict[str, int]],
    member_name: str,
    destination_path: Path,
) -> None:
    if member_name not in entries:
        raise FileNotFoundError(f"ZIP member not found: {member_name}")

    entry = entries[member_name]
    local_header_offset = entry["local_header_offset"]
    local_header = _download_byte_range(
        session, url, local_header_offset, local_header_offset + 29
    )
    if local_header[:4] != b"PK\x03\x04":
        raise ValueError("Invalid ZIP local file header.")

    (
        _signature,
        _version_needed,
        _flags,
        compression_method,
        _mod_time,
        _mod_date,
        _crc32,
        _compressed_size,
        _uncompressed_size,
        filename_length,
        extra_length,
    ) = struct.unpack("<4s5H3L2H", local_header)

    data_start = local_header_offset + 30 + filename_length + extra_length
    data_end = data_start + entry["compressed_size"] - 1
    # Guard against bad ZIP64 / CDN length mismatches
    file_size, _ = _head_file(session, url)
    if data_start < 0 or data_end >= file_size or data_end < data_start:
        raise ValueError(
            f"Computed byte range {data_start}-{data_end} is outside file "
            f"(size={file_size}) for {member_name}."
        )
    compressed_data = _download_byte_range(session, url, data_start, data_end)

    if compression_method == 0:
        data = compressed_data
    elif compression_method == 8:
        data = zlib.decompress(compressed_data, -zlib.MAX_WBITS)
    else:
        raise ValueError(f"Unsupported ZIP compression method: {compression_method}")

    # GeoTIFF magic: little-endian II*\0 or big-endian MM\0*
    if len(data) < 4 or data[:2] not in (b"II", b"MM"):
        raise ValueError(
            f"Extracted member {member_name} is not a TIFF "
            f"(got magic={data[:8]!r})."
        )

    destination_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = destination_path.with_suffix(destination_path.suffix + ".part")
    tmp_path.write_bytes(data)
    tmp_path.replace(destination_path)


def _merge_rasters(
    output_path: Path, tiles: List[Path], bounds: Tuple[float, float, float, float]
) -> None:
    rasters = [rasterio.open(tile) for tile in tiles]
    try:
        merged_raster, merged_transform = rasterio.merge.merge(rasters, bounds=bounds)
        source_crs = rasters[0].crs
        if source_crs is None:
            raise ValueError("No CRS present in FABDEM tile metadata.")
        metadata = {
            "count": merged_raster.shape[0],
            "height": merged_raster.shape[1],
            "width": merged_raster.shape[2],
            "dtype": merged_raster.dtype,
            "crs": source_crs,
            "transform": merged_transform,
        }
        with rasterio.open(output_path, mode="w", **metadata) as dest:
            dest.write(merged_raster)
    finally:
        for raster in rasters:
            raster.close()


def _download_fabdem_for_bounds(
    bounds: Tuple[float, float, float, float],
    output_path: Path,
    cache_dir: Path,
) -> Dict[str, Any]:
    """Download only intersecting TIFF tiles via HTTP Range (no full ZIP)."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    purged = _purge_invalid_cache(cache_dir)
    if purged:
        logger.warning("Purged invalid FABDEM cache zip(s): %s", ", ".join(purged))

    rect = shapely.geometry.box(*bounds)

    with _session() as session:
        response = session.get(TILES_GEOJSON, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        tiles_gdf = gpd.GeoDataFrame.from_features(
            response.json()["features"], crs=4326
        )
        intersecting = tiles_gdf[tiles_gdf.geometry.intersects(rect)]
        if intersecting.empty:
            raise RuntimeError(
                "No FABDEM tiles intersect the uploaded KML bounding box."
            )

        grouped: Dict[str, List[str]] = {}
        for row in intersecting.itertuples():
            zip_name = _normalize_zip_name(row.zipfile_name)
            member = _correct_tile_name(row.file_name)
            grouped.setdefault(zip_name, []).append(member)

        zip_index_cache: Dict[str, Dict[str, Dict[str, int]]] = {}
        for zip_name, member_names in grouped.items():
            tile_url = f"{FABDEM_BASE_URL}/{zip_name}"
            for member_name in member_names:
                dest = cache_dir / member_name
                if dest.exists() and dest.stat().st_size > 1024:
                    # Quick TIFF magic check
                    with open(dest, "rb") as fh:
                        if fh.read(2) in (b"II", b"MM"):
                            continue
                    dest.unlink(missing_ok=True)

                if tile_url not in zip_index_cache:
                    zip_index_cache[tile_url] = _read_remote_zip_index(
                        session, tile_url
                    )
                try:
                    _extract_remote_zip_member(
                        session,
                        tile_url,
                        zip_index_cache[tile_url],
                        member_name,
                        dest,
                    )
                except Exception as exc:
                    raise RuntimeError(
                        f"Failed to extract FABDEM tile {member_name} from "
                        f"{zip_name}: {exc}. "
                        "The Bristol data host may be slow/unreachable from "
                        "Railway, or the response was not a valid ZIP member."
                    ) from exc

        tile_paths = [
            cache_dir / _correct_tile_name(name)
            for name in intersecting.file_name
        ]
        missing = [p for p in tile_paths if not p.exists()]
        if missing:
            raise RuntimeError(
                "FABDEM tile download incomplete: "
                + ", ".join(p.name for p in missing)
            )

        _merge_rasters(output_path, tile_paths, bounds)
        return {
            "tile_count": len(tile_paths),
            "zip_count": len(grouped),
            "zip_names": sorted(grouped.keys()),
            "cache_purged": purged,
        }


def _smooth_dtm(
    src_path: Path, dst_path: Path, aoi_geometry: Any
) -> Dict[str, Any]:
    """Upsample + Gaussian-smooth a clipped DTM, re-clipped to the KML geometry."""
    with rasterio.open(src_path) as src:
        data = src.read(1).astype(np.float32)
        nodata = src.nodata
        height, width = data.shape
        inside = geometry_mask(
            [aoi_geometry.__geo_interface__],
            out_shape=(height, width),
            transform=src.transform,
            invert=True,
            all_touched=True,
        )
        valid = inside & np.isfinite(data)
        if nodata is not None:
            valid &= data != nodata

        upscale = DTM_SMOOTH_UPSCALE
        while upscale > 1 and height * width * upscale * upscale > DTM_SMOOTH_MAX_PIXELS:
            upscale -= 1
        out_h, out_w = height * upscale, width * upscale
        out_transform = src.transform * Affine.scale(1.0 / upscale)

        src_filled = np.where(valid, data, DTM_NODATA).astype(np.float32)
        upsampled = np.full((out_h, out_w), DTM_NODATA, dtype=np.float32)
        reproject(
            source=src_filled,
            destination=upsampled,
            src_transform=src.transform,
            src_crs=src.crs,
            src_nodata=DTM_NODATA,
            dst_transform=out_transform,
            dst_crs=src.crs,
            dst_nodata=DTM_NODATA,
            resampling=Resampling.bilinear,
        )

        up_valid = upsampled != DTM_NODATA
        weights = up_valid.astype(np.float32)
        numerator = gaussian_filter(np.where(up_valid, upsampled, 0.0), DTM_SMOOTH_SIGMA_PX)
        denominator = gaussian_filter(weights, DTM_SMOOTH_SIGMA_PX)
        with np.errstate(invalid="ignore", divide="ignore"):
            smoothed = np.where(
                denominator > 1e-6, numerator / np.maximum(denominator, 1e-6), DTM_NODATA
            ).astype(np.float32)

        out_inside = geometry_mask(
            [aoi_geometry.__geo_interface__],
            out_shape=(out_h, out_w),
            transform=out_transform,
            invert=True,
        )
        keep = out_inside & (denominator > 1e-6)
        smoothed = np.where(keep, smoothed, DTM_NODATA).astype(np.float32)

        meta = src.meta.copy()
        meta.update(
            {
                "driver": "GTiff",
                "dtype": "float32",
                "nodata": DTM_NODATA,
                "height": out_h,
                "width": out_w,
                "transform": out_transform,
                "compress": "deflate",
            }
        )
        with rasterio.open(dst_path, "w", **meta) as dst:
            dst.write(smoothed, 1)

    return {
        "upscale": upscale,
        "sigma_px": DTM_SMOOTH_SIGMA_PX,
        "width": out_w,
        "height": out_h,
        "nodata": DTM_NODATA,
    }


def download_fabdem_dtm_from_kml(kml_bytes: bytes) -> Dict[str, Any]:
    """
    Download FABDEM for the KML bbox and clip to the KML boundary.

    Returns metadata + clipped GeoTIFF bytes (logic matches the standalone script).
    """
    if not kml_bytes:
        raise ValueError("KML file is empty.")

    # Persistent tile cache (same idea as FABDEM_Cache in the script)
    project_root = Path(__file__).resolve().parent.parent
    cache_dir = project_root / "FABDEM_Cache"
    cache_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="fabdem_") as tmp:
        tmp_dir = Path(tmp)
        kml_path = tmp_dir / "upload.kml"
        output_dir = tmp_dir / "FABDEM_Output"
        output_dir.mkdir(parents=True, exist_ok=True)

        temp_file = output_dir / "FABDEM_Temporary.tif"
        output_file = output_dir / "FABDEM_DTM_KML_Clipped.tif"

        kml_path.write_bytes(kml_bytes)

        # ---- read KML ----
        try:
            aoi = gpd.read_file(kml_path, driver="KML")
        except Exception:
            # Fallback without explicit driver (pyogrio / fiona auto)
            try:
                aoi = gpd.read_file(kml_path)
            except Exception as error2:
                raise ValueError(f"Failed to read KML: {error2}") from error2

        if aoi.empty:
            raise ValueError("KML contains no geometry.")

        aoi = aoi[aoi.geometry.notnull()]
        if aoi.empty:
            raise ValueError("No valid geometry found in KML.")

        # ---- CRS → WGS84 (FABDEM bounds must be lon/lat) ----
        if aoi.crs is None:
            aoi = aoi.set_crs("EPSG:4326")
        else:
            aoi = aoi.to_crs("EPSG:4326")

        # ---- merge geometries + bbox ----
        aoi_geometry = aoi.geometry.union_all()
        west, south, east, north = aoi_geometry.bounds
        bounds = (float(west), float(south), float(east), float(north))

        # ---- FABDEM download (range-only; never full multi-GB ZIP) ----
        try:
            dl_meta = _download_fabdem_for_bounds(bounds, temp_file, cache_dir)
        except Exception as error:
            msg = str(error)
            if "not a zip file" in msg.lower() or "BadZipFile" in type(error).__name__:
                raise RuntimeError(
                    "FABDEM download failed: received a non-ZIP response "
                    "(often a truncated download or HTML error page). "
                    "India tiles sit in ~1.3 GB archives; this API now pulls "
                    "only the needed TIFF via HTTP Range. Retry the request; "
                    f"if it persists, Bristol host may be blocking Railway. "
                    f"Detail: {error}"
                ) from error
            raise RuntimeError(f"FABDEM download failed: {error}") from error

        if not temp_file.exists():
            raise RuntimeError("FABDEM download produced no temporary TIFF.")

        # ---- clip to exact KML boundary ----
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
            raise RuntimeError(f"FABDEM clipping failed: {error}") from error

        smoothed_file = output_dir / "FABDEM_DTM_KML_Clipped_Smoothed.tif"
        try:
            smoothing = _smooth_dtm(output_file, smoothed_file, aoi_geometry)
        except Exception as error:
            raise RuntimeError(f"FABDEM edge smoothing failed: {error}") from error

        raw_tif_bytes = output_file.read_bytes()
        tif_bytes = smoothed_file.read_bytes()

        return {
            "bounds": {
                "west": float(west),
                "south": float(south),
                "east": float(east),
                "north": float(north),
            },
            "geometry_type": str(aoi_geometry.geom_type),
            "crs": "EPSG:4326",
            "width": int(smoothing["width"]),
            "height": int(smoothing["height"]),
            "raw_width": int(clipped.shape[2]),
            "raw_height": int(clipped.shape[1]),
            "tif_filename": "FABDEM_DTM_KML_Clipped.tif",
            "tif_bytes": tif_bytes,
            "raw_tif_filename": "FABDEM_DTM_KML_Clipped_Raw.tif",
            "raw_tif_bytes": raw_tif_bytes,
            "smoothing": {
                "method": (
                    "Bilinear upsample + nodata-aware Gaussian blur, re-clipped "
                    "to KML — removes square 30 m cell steps (continuous slopes)."
                ),
                **smoothing,
            },
            "download": dl_meta,
            "notes": {
                "source": "FABDEM (Forest And Buildings removed Copernicus DEM)",
                "clip": "Clipped to exact uploaded KML geometry (not only bbox).",
                "method": (
                    "HTTP Range extraction of required TIFF members only "
                    "(avoids full multi-GB ZIP download that fails on Railway)."
                ),
            },
        }
