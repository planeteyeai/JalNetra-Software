"""
kml_pixel_smoother.py
----------------------
Converts a "blocky pixel" classification KML (e.g. exported from Google Earth
Engine, where every Placemark is one raster pixel/merged pixel-block with a
hard-edged solid fill colour) into a single smoothed, gradient-coloured
raster image and re-packages it as a KMZ GroundOverlay.

Also post-processes JalNetra API GroundOverlay KMLs (embedded base64 PNG) so
downloaded overlays get soft edges and continuous colour blending without
changing GEE classification logic.

USAGE (polygon placemark KML → KMZ)
-----
    python -m jalnetra.kml_pixel_smoother input.kml output.kmz \
        --opacity 0.70 --color-blur 1.8 --edge-blur 3.0 \
        --saturation 1.6 --brightness 1.05 --upscale 3

API helper
----------
    from jalnetra.kml_pixel_smoother import smooth_kml_bytes
    smoothed = smooth_kml_bytes(kml_bytes)  # GroundOverlay KML in → KML out
"""

from __future__ import annotations

import argparse
import base64
import io
import os
import re
import zipfile
from typing import Optional, Tuple

import numpy as np
from PIL import Image
from scipy.ndimage import gaussian_filter

# Upscaled overlay size cap (keeps API memory and embedded-KML size bounded).
MAX_SMOOTHED_PIXELS = 24_000_000

# --------------------------------------------------------------------------
# HSV helpers (avoid hard matplotlib dependency for the API path)
# --------------------------------------------------------------------------

def _rgb_to_hsv(rgb: np.ndarray) -> np.ndarray:
    """rgb float [0,1] HxWx3 → hsv float [0,1] HxWx3."""
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    maxc = np.maximum(np.maximum(r, g), b)
    minc = np.minimum(np.minimum(r, g), b)
    v = maxc
    deltac = maxc - minc
    s = np.where(maxc > 1e-8, deltac / np.maximum(maxc, 1e-8), 0.0)
    rc = (maxc - r) / np.maximum(deltac, 1e-8)
    gc = (maxc - g) / np.maximum(deltac, 1e-8)
    bc = (maxc - b) / np.maximum(deltac, 1e-8)
    h = np.zeros_like(maxc)
    h = np.where((r == maxc) & (deltac > 0), bc - gc, h)
    h = np.where((g == maxc) & (deltac > 0), 2.0 + rc - bc, h)
    h = np.where((b == maxc) & (deltac > 0), 4.0 + gc - rc, h)
    h = (h / 6.0) % 1.0
    return np.dstack([h, s, v])


def _hsv_to_rgb(hsv: np.ndarray) -> np.ndarray:
    """hsv float [0,1] HxWx3 → rgb float [0,1] HxWx3."""
    h, s, v = hsv[..., 0], hsv[..., 1], hsv[..., 2]
    i = np.floor(h * 6.0).astype(int)
    f = h * 6.0 - i
    p = v * (1.0 - s)
    q = v * (1.0 - s * f)
    t = v * (1.0 - s * (1.0 - f))
    i_mod = i % 6
    r = np.choose(i_mod, [v, q, p, p, t, v])
    g = np.choose(i_mod, [t, v, v, q, p, p])
    b = np.choose(i_mod, [p, p, t, v, v, q])
    return np.dstack([r, g, b])


# --------------------------------------------------------------------------
# 1. Parse the KML (polygon placemark path)
# --------------------------------------------------------------------------

def kml_color_to_rgba(hexcolor: str):
    """KML colour string is aabbggrr (each 2 hex digits)."""
    hexcolor = hexcolor.strip()
    a = int(hexcolor[0:2], 16)
    b = int(hexcolor[2:4], 16)
    g = int(hexcolor[4:6], 16)
    r = int(hexcolor[6:8], 16)
    return r, g, b, a


def parse_kml(path: str):
    data = open(path, encoding="utf-8").read()

    style_matches = re.findall(
        r'<Style id="(\w+)">.*?<PolyStyle id="\w+">\s*<color>([0-9a-fA-F]{8})</color>',
        data, re.S)
    styles = {sid: kml_color_to_rgba(col) for sid, col in style_matches}

    pm_pattern = re.compile(
        r'<Placemark[^>]*>\s*<name>(.*?)</name>\s*'
        r'<description><!\[CDATA\[(.*?)\]\]></description>\s*'
        r'<styleUrl>#(\w+)</styleUrl>.*?'
        r'<coordinates>(.*?)</coordinates>',
        re.S)

    placemarks = []
    for name, desc, styleid, coordtext in pm_pattern.findall(data):
        cls = re.sub(r'\s*\d+$', '', name).strip()
        rng = re.search(r'Range:\s*(-?[\d.]+)\s*to\s*(-?[\d.]+)', desc)
        value = (float(rng.group(1)) + float(rng.group(2))) / 2.0 if rng else 0.0
        color = styles.get(styleid, (128, 128, 128, 230))
        pts = []
        for tok in coordtext.split():
            parts = tok.split(',')
            lon, lat = float(parts[0]), float(parts[1])
            pts.append((lon, lat))
        if len(pts) >= 3:
            placemarks.append({
                "class": cls, "value": value, "color": color, "coords": pts
            })

    if not placemarks:
        raise ValueError(
            "No <Placemark><Polygon> features with a parsable "
            "Range/description were found in this KML."
        )
    return placemarks


def build_color_ramp(placemarks):
    """One representative colour per class, ordered by value -> for np.interp."""
    by_class = {}
    for p in placemarks:
        by_class.setdefault(p["class"], []).append((p["value"], p["color"]))
    ramp = []
    for cls, entries in by_class.items():
        value = entries[0][0]
        color = entries[0][1]
        ramp.append((value, color, cls))
    ramp.sort(key=lambda t: t[0])
    values = np.array([v for v, c, n in ramp], dtype=float)
    r = np.array([c[0] for v, c, n in ramp], dtype=float)
    g = np.array([c[1] for v, c, n in ramp], dtype=float)
    b = np.array([c[2] for v, c, n in ramp], dtype=float)
    print("Colour ramp (auto-detected from your KML):")
    for v, c, n in ramp:
        print(f"    {n:<22} value={v:+.2f}  RGB={c[:3]}")
    return values, r, g, b


# --------------------------------------------------------------------------
# 2. Rasterise
# --------------------------------------------------------------------------

def native_pixel_size(placemarks, sample=4000):
    """Estimate the KML's native pixel size in degrees from vertex spacing."""
    deltas = []
    n = 0
    for p in placemarks:
        xs = sorted(set(round(x, 10) for x, y in p["coords"]))
        ys = sorted(set(round(y, 10) for x, y in p["coords"]))
        for a, b in zip(xs, xs[1:]):
            d = b - a
            if d > 1e-9:
                deltas.append(d)
        for a, b in zip(ys, ys[1:]):
            d = b - a
            if d > 1e-9:
                deltas.append(d)
        n += 1
        if n >= sample:
            break
    deltas = np.array(deltas)
    return float(np.percentile(deltas, 10))


def rasterize(placemarks, values, r, g, b, pixel_size, padding_px=4):
    lons = [x for p in placemarks for x, y in p["coords"]]
    lats = [y for p in placemarks for x, y in p["coords"]]
    min_lon, max_lon = min(lons), max(lons)
    min_lat, max_lat = min(lats), max(lats)

    width = int(np.ceil((max_lon - min_lon) / pixel_size)) + 2 * padding_px
    height = int(np.ceil((max_lat - min_lat) / pixel_size)) + 2 * padding_px
    min_lon -= padding_px * pixel_size
    max_lat += padding_px * pixel_size

    value_grid = np.zeros((height, width), dtype=float)
    coverage = np.zeros((height, width), dtype=float)

    def to_px(lon, lat):
        col = (lon - min_lon) / pixel_size
        row = (max_lat - lat) / pixel_size
        return col, row

    from PIL import ImageDraw
    for p in placemarks:
        poly_px = [to_px(lon, lat) for lon, lat in p["coords"]]
        xs = [c for c, r_ in poly_px]
        ys = [r_ for c, r_ in poly_px]
        x0 = max(int(np.floor(min(xs))) - 1, 0)
        x1 = min(int(np.ceil(max(xs))) + 1, width)
        y0 = max(int(np.floor(min(ys))) - 1, 0)
        y1 = min(int(np.ceil(max(ys))) + 1, height)
        w, h = x1 - x0, y1 - y0
        if w <= 0 or h <= 0:
            continue
        local_poly = [(c - x0, r_ - y0) for c, r_ in poly_px]
        mask_img = Image.new("L", (w, h), 0)
        ImageDraw.Draw(mask_img).polygon(local_poly, fill=255)
        m = np.array(mask_img, dtype=bool)
        value_grid[y0:y1, x0:x1][m] = p["value"]
        coverage[y0:y1, x0:x1][m] = 1.0

    bounds = (min_lon, min_lon + width * pixel_size,
              max_lat - height * pixel_size, max_lat)  # west, east, south, north
    return value_grid, coverage, bounds


# --------------------------------------------------------------------------
# 3. Coverage-aware smoothing + colour mapping
# --------------------------------------------------------------------------

def smooth_and_colorize(value_grid, coverage, ramp_values, ramp_r, ramp_g, ramp_b,
                         color_sigma, edge_sigma, opacity, saturation=1.0, brightness=1.0):
    """
    Two separate blur passes:
    - color_sigma blurs the value that picks the colour
    - edge_sigma blurs only the coverage mask (alpha)
    """
    weighted_val = gaussian_filter(value_grid * coverage, color_sigma)
    color_weight = gaussian_filter(coverage, color_sigma)
    with np.errstate(invalid="ignore", divide="ignore"):
        smooth_value = np.where(
            color_weight > 1e-6,
            weighted_val / np.maximum(color_weight, 1e-6),
            0.0,
        )

    edge_weight = gaussian_filter(coverage, edge_sigma)

    r = np.interp(smooth_value, ramp_values, ramp_r) / 255.0
    g = np.interp(smooth_value, ramp_values, ramp_g) / 255.0
    b = np.interp(smooth_value, ramp_values, ramp_b) / 255.0

    if saturation != 1.0 or brightness != 1.0:
        hsv = _rgb_to_hsv(np.dstack([r, g, b]))
        hsv[..., 1] = np.clip(hsv[..., 1] * saturation, 0, 1)
        hsv[..., 2] = np.clip(hsv[..., 2] * brightness, 0, 1)
        rgb_boosted = _hsv_to_rgb(hsv)
        r, g, b = rgb_boosted[..., 0], rgb_boosted[..., 1], rgb_boosted[..., 2]

    r = np.clip(r * 255.0, 0, 255)
    g = np.clip(g * 255.0, 0, 255)
    b = np.clip(b * 255.0, 0, 255)
    a = np.clip(edge_weight, 0, 1) * opacity * 255.0

    rgba = np.dstack([r, g, b, a]).astype(np.uint8)
    return rgba


def smooth_rgba_png(
    png_bytes: bytes,
    *,
    color_sigma: float = 1.8,
    edge_sigma: float = 3.0,
    opacity: float = 0.70,
    saturation: float = 1.6,
    brightness: float = 1.05,
    upscale: int = 3,
) -> bytes:
    """
    Coverage-aware smooth of an existing classification overlay PNG
    (JalNetra GroundOverlay thumbnails). Softens hard pixel edges without
    bleeding colour into transparent background.
    """
    img = Image.open(io.BytesIO(png_bytes)).convert("RGBA")
    arr = np.asarray(img).astype(np.float32)
    rgb = arr[..., :3]
    alpha = arr[..., 3] / 255.0
    coverage = (alpha > 0.01).astype(np.float32)

    # Prefer alpha as coverage when present; else opaque non-black pixels
    if float(alpha.max()) < 0.01:
        coverage = (np.any(rgb > 2.0, axis=-1)).astype(np.float32)
        alpha = coverage.copy()

    cov = np.maximum(coverage * np.maximum(alpha, coverage), 0.0)

    smooth_rgb = np.zeros_like(rgb)
    color_weight = gaussian_filter(cov, color_sigma)
    for c in range(3):
        weighted = gaussian_filter(rgb[..., c] * cov, color_sigma)
        with np.errstate(invalid="ignore", divide="ignore"):
            smooth_rgb[..., c] = np.where(
                color_weight > 1e-6,
                weighted / np.maximum(color_weight, 1e-6),
                0.0,
            )

    edge_weight = gaussian_filter(cov, edge_sigma)

    r = smooth_rgb[..., 0] / 255.0
    g = smooth_rgb[..., 1] / 255.0
    b = smooth_rgb[..., 2] / 255.0

    if saturation != 1.0 or brightness != 1.0:
        hsv = _rgb_to_hsv(np.dstack([r, g, b]))
        hsv[..., 1] = np.clip(hsv[..., 1] * saturation, 0, 1)
        hsv[..., 2] = np.clip(hsv[..., 2] * brightness, 0, 1)
        rgb_boosted = _hsv_to_rgb(hsv)
        r, g, b = rgb_boosted[..., 0], rgb_boosted[..., 1], rgb_boosted[..., 2]

    out = np.dstack([
        np.clip(r * 255.0, 0, 255),
        np.clip(g * 255.0, 0, 255),
        np.clip(b * 255.0, 0, 255),
        np.clip(edge_weight, 0, 1) * opacity * 255.0,
    ]).astype(np.uint8)

    out_img = Image.fromarray(out, mode="RGBA")
    while upscale and upscale > 1 and (
        out_img.width * out_img.height * upscale * upscale > MAX_SMOOTHED_PIXELS
    ):
        upscale -= 1
    if upscale and upscale != 1:
        out_img = out_img.resize(
            (out_img.width * upscale, out_img.height * upscale),
            Image.LANCZOS,
        )
    buf = io.BytesIO()
    out_img.save(buf, format="PNG")
    return buf.getvalue()


_DATA_PNG_RE = re.compile(
    r"(data:image/png;base64,)([A-Za-z0-9+/=\s]+)",
    re.IGNORECASE,
)


def smooth_kml_bytes(
    kml_bytes: bytes,
    *,
    color_sigma: float = 1.8,
    edge_sigma: float = 3.0,
    opacity: float = 0.70,
    saturation: float = 1.6,
    brightness: float = 1.05,
    upscale: int = 3,
) -> bytes:
    """
    Post-process a JalNetra GroundOverlay KML: smooth every embedded PNG and
    re-embed as base64. Non-overlay / point KMLs are returned unchanged.
    Classification / GEE logic is not touched — PNG only.
    """
    try:
        text = kml_bytes.decode("utf-8")
    except UnicodeDecodeError:
        text = kml_bytes.decode("utf-8", errors="replace")

    if "data:image/png;base64," not in text.lower():
        return kml_bytes

    def _repl(match: re.Match) -> str:
        prefix = match.group(1)
        b64 = re.sub(r"\s+", "", match.group(2))
        try:
            raw = base64.b64decode(b64)
            smoothed = smooth_rgba_png(
                raw,
                color_sigma=color_sigma,
                edge_sigma=edge_sigma,
                opacity=opacity,
                saturation=saturation,
                brightness=brightness,
                upscale=upscale,
            )
            return prefix + base64.b64encode(smoothed).decode("ascii")
        except Exception:
            return match.group(0)

    new_text = _DATA_PNG_RE.sub(_repl, text)
    return new_text.encode("utf-8")


# --------------------------------------------------------------------------
# 4. Write KMZ (PNG + GroundOverlay KML, zipped)
# --------------------------------------------------------------------------

def write_kmz(rgba, bounds, out_path, doc_name, doc_desc, upscale=1):
    west, east, south, north = bounds
    img = Image.fromarray(rgba, mode="RGBA")
    if upscale and upscale != 1:
        img = img.resize((img.width * upscale, img.height * upscale), Image.LANCZOS)

    tmp_dir = os.path.dirname(out_path) or "."
    png_name = "overlay.png"
    png_path = os.path.join(tmp_dir, png_name)
    img.save(png_path)

    kml_text = f"""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
<Document>
  <name>{doc_name}</name>
  <description><![CDATA[{doc_desc}]]></description>
  <GroundOverlay>
    <name>{doc_name}</name>
    <description><![CDATA[Smoothed raster overlay: pixel blocks blurred into a
continuous gradient, boundary/edges softened, opacity applied.]]></description>
    <Icon>
      <href>{png_name}</href>
    </Icon>
    <LatLonBox>
      <north>{north:.10f}</north>
      <south>{south:.10f}</south>
      <east>{east:.10f}</east>
      <west>{west:.10f}</west>
    </LatLonBox>
  </GroundOverlay>
</Document>
</kml>
"""
    kml_path = os.path.join(tmp_dir, "doc.kml")
    with open(kml_path, "w", encoding="utf-8") as f:
        f.write(kml_text)

    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(kml_path, "doc.kml")
        z.write(png_path, png_name)

    os.remove(kml_path)
    os.remove(png_path)


# --------------------------------------------------------------------------
# main (CLI: polygon placemark KML → KMZ)
# --------------------------------------------------------------------------

def main(argv: Optional[list] = None):
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("input_kml")
    ap.add_argument("output_kmz")
    ap.add_argument("--opacity", type=float, default=0.70,
                    help="Final overlay opacity, 0-1 (default 0.70 = 70%%)")
    ap.add_argument("--color-blur", type=float, default=1.8, dest="color_blur",
                    help="Gaussian blur sigma (native pixels) for COLOUR only")
    ap.add_argument("--edge-blur", type=float, default=3.0, dest="edge_blur",
                    help="Gaussian blur sigma (native pixels) for MASK edge/alpha")
    ap.add_argument("--saturation", type=float, default=1.6,
                    help="Saturation multiplier after colouring (default 1.6)")
    ap.add_argument("--brightness", type=float, default=1.05,
                    help="Value/brightness multiplier (default 1.05)")
    ap.add_argument("--upscale", type=int, default=3,
                    help="Integer upscale after smoothing (default 3)")
    ap.add_argument(
        "--groundoverlay",
        action="store_true",
        help="Treat input as JalNetra GroundOverlay KML; write smoothed .kml "
             "(output path should end in .kml)",
    )
    args = ap.parse_args(argv)

    if args.groundoverlay:
        print(f"Reading GroundOverlay KML {args.input_kml} ...")
        raw = open(args.input_kml, "rb").read()
        out = smooth_kml_bytes(
            raw,
            color_sigma=args.color_blur,
            edge_sigma=args.edge_blur,
            opacity=args.opacity,
            saturation=args.saturation,
            brightness=args.brightness,
            upscale=args.upscale,
        )
        with open(args.output_kmz, "wb") as f:
            f.write(out)
        print(f"Wrote {args.output_kmz}")
        return

    print(f"Reading {args.input_kml} ...")
    placemarks = parse_kml(args.input_kml)
    print(f"Parsed {len(placemarks)} pixel/polygon features.")

    ramp_values, ramp_r, ramp_g, ramp_b = build_color_ramp(placemarks)

    pixel_size = native_pixel_size(placemarks)
    print(f"Detected native pixel size: {pixel_size:.8f} deg "
          f"(~{pixel_size * 111320:.2f} m)")

    value_grid, coverage, bounds = rasterize(
        placemarks, ramp_values, ramp_r, ramp_g, ramp_b, pixel_size)
    print(f"Rasterised to grid {value_grid.shape[1]} x {value_grid.shape[0]} px")

    rgba = smooth_and_colorize(
        value_grid, coverage, ramp_values,
        ramp_r, ramp_g, ramp_b,
        color_sigma=args.color_blur, edge_sigma=args.edge_blur,
        opacity=args.opacity,
        saturation=args.saturation, brightness=args.brightness,
    )

    write_kmz(
        rgba, bounds, args.output_kmz,
        doc_name="Smoothed classification overlay",
        doc_desc="Auto-generated: pixel blocks smoothed into a continuous "
                 "gradient raster, boundaries removed, opacity applied.",
        upscale=args.upscale,
    )
    print(f"Wrote {args.output_kmz}")


if __name__ == "__main__":
    main()
