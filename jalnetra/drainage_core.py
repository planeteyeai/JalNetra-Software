"""
drainage_core.py
================
River polygon (KML / KMZ)  ->  MERIT Hydro (upa + D8 dir + elv)  ->  main stream + upstreams  ->  KML / GeoJSON

Method
------
1. Read the river polygon(s) from the KML and build an area of interest (AOI):
   polygon bounding box + `buffer_m` metres (default 2 km, like your Drainage_Lines file).
2. Download MERIT Hydro v1.0.1 for the AOI on its native 3-arc-second grid (~90 m):
     upa = upstream drainage area (km²),  dir = D8 flow direction,  elv = elevation (m).
3. Stream pixels  = upa >= threshold_km2.  Each stream pixel is linked to its downstream
   neighbour with the D8 `dir` band, so the network topology comes straight from MERIT.
4. Main stream: the outlet is the stream pixel with the largest upa inside the polygon
   (+ `snap_m` tolerance). From there the trace goes upstream, at every junction picking the
   branch that stays inside the polygon longest (ties -> larger upa), and stops where the
   polygon ends.
5. Upstreams: every other stream pixel whose flow path reaches the main stream (or ends inside
   the polygon) is an upstream. They are grouped into whole tributaries: at each junction the
   larger branch keeps the tributary's id and smaller branches become new tributaries one level
   higher. Level 1 = drains directly into the main stream.
6. Only drainage that belongs to the river is kept: the main stream is trimmed to the polygon and
   kept inside its banks; upstreams beyond either end of the river (e.g. the river's own network
   above the polygon) are removed.
7. Output: two KMLs. main_stream.kml = the main stream in the river polygon.
   upstreams.kml = one placemark per upstream joining the river, with all of its branches combined.
"""
from __future__ import annotations

import io
import math
import re
import zipfile
from collections import deque
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from xml.sax.saxutils import escape

import numpy as np
import shapely
from pyproj import Geod, Transformer
from shapely.geometry import LineString, MultiLineString, MultiPolygon, Point, Polygon, mapping, shape
from shapely.ops import nearest_points, substring, unary_union
from shapely.validation import make_valid

try:  # safer XML parsing for uploaded files
    from defusedxml.ElementTree import fromstring as _xml_fromstring
except ImportError:  # pragma: no cover
    from xml.etree.ElementTree import fromstring as _xml_fromstring

MERIT_ASSET = "MERIT/Hydro/v1_0_1"
RES = 1.0 / 1200.0  # MERIT Hydro native resolution: 3 arc-seconds
NODATA = -9999.0
MAX_AOI_PIXELS = 25_000_000

# MERIT Hydro D8 codes -> (row offset, col offset); rows grow southwards
D8 = {1: (0, 1), 2: (1, 1), 4: (1, 0), 8: (1, -1), 16: (0, -1), 32: (-1, -1), 64: (-1, 0), 128: (-1, 1)}
D8_CODE = {v: k for k, v in D8.items()}
GEOD = Geod(ellps="WGS84")


class DrainageError(Exception):
    """Error with an HTTP-style status code so the API can pass it straight through."""

    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


@dataclass
class Params:
    threshold_km2: float = 1.0   # min upstream area for a pixel to count as a stream
    buffer_m: float = 2000.0     # how far around the polygon to look for upstreams
    snap_m: float = 150.0        # tolerance between the polygon and the 90 m MERIT channel
    min_length_m: float = 0.0    # drop end-of-network upstreams shorter than this
    smooth: bool = True          # Chaikin smoothing of the 90 m staircase lines
    include_other: bool = False  # also output streams in the AOI that do NOT reach the river
    river_name: str | None = None

    def validate(self):
        if not 0.01 <= self.threshold_km2 <= 100000:
            raise DrainageError("threshold_km2 must be between 0.01 and 100000", 400)
        if not 0 <= self.buffer_m <= 50000:
            raise DrainageError("buffer_m must be between 0 and 50000", 400)
        if not 0 <= self.snap_m <= 2000:
            raise DrainageError("snap_m must be between 0 and 2000", 400)
        if self.min_length_m < 0:
            raise DrainageError("min_length_m must be >= 0", 400)


# --------------------------------------------------------------------------------------
# KML input
# --------------------------------------------------------------------------------------
def _local(tag) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _parse_coords(text: str):
    pts = []
    for tok in text.split():
        parts = tok.split(",")
        if len(parts) >= 2:
            pts.append((float(parts[0]), float(parts[1])))
    return pts


def _clean_name(name: str | None) -> str:
    name = re.sub(r"\.km[lz]$", "", (name or "").strip(), flags=re.I)
    name = re.sub(r"[_\s]+", " ", name).strip()
    if name.isupper():
        name = name.title()
    return name


def read_river_kml(data: bytes, filename: str = "river.kml"):
    """Return (river_name, polygon_geometry) from KML or KMZ bytes."""
    if data[:2] == b"PK":
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                kmls = [n for n in z.namelist() if n.lower().endswith(".kml")]
                if not kmls:
                    raise DrainageError("The KMZ does not contain a .kml file.", 400)
                data = z.read("doc.kml" if "doc.kml" in kmls else kmls[0])
        except zipfile.BadZipFile as e:
            raise DrainageError(f"Could not open the KMZ: {e}", 400)
    try:
        root = _xml_fromstring(data)
    except Exception as e:
        raise DrainageError(f"Could not read the KML: {e}", 400)

    polys, names, doc_name = [], [], None
    for el in root.iter():
        t = _local(el.tag)
        if t == "Document" and doc_name is None:
            n = next((c for c in el if _local(c.tag) == "name"), None)
            doc_name = n.text if n is not None else None
        elif t == "Placemark":
            n = next((c for c in el if _local(c.tag) == "name"), None)
            if n is not None and n.text:
                names.append(n.text)
        elif t == "Polygon":
            outer, holes = None, []
            for b in el:
                bt = _local(b.tag)
                if bt not in ("outerBoundaryIs", "innerBoundaryIs"):
                    continue
                c = next((x for x in b.iter() if _local(x.tag) == "coordinates"), None)
                if c is None or not c.text:
                    continue
                ring = _parse_coords(c.text)
                if len(ring) < 3:
                    continue
                if bt == "outerBoundaryIs":
                    outer = ring
                else:
                    holes.append(ring)
            if outer:
                polys.append(Polygon(outer, holes))

    if not polys:
        raise DrainageError(
            "No polygon found in the KML. Upload the river as a closed polygon "
            "(in Google Earth: Add > Polygon around the river banks).", 400)

    geom = make_valid(unary_union([p if p.is_valid else make_valid(p) for p in polys]))
    if geom.geom_type == "GeometryCollection":
        geom = unary_union([g for g in geom.geoms if g.geom_type in ("Polygon", "MultiPolygon")])
    if geom.is_empty or geom.geom_type not in ("Polygon", "MultiPolygon"):
        raise DrainageError("The polygon in the KML is empty or invalid.", 400)

    generic = {"", "untitled polygon", "untitled path", "polygon", "untitled"}
    river_name = "River"
    for cand in [doc_name, *names, Path(filename).stem]:
        c = _clean_name(cand)
        if c.lower() not in generic:
            river_name = c
            break
    return river_name, geom


# --------------------------------------------------------------------------------------
# Geometry helpers
# --------------------------------------------------------------------------------------
class LocalProjection:
    """UTM projection around the river, for metre-based buffers and centrelines."""

    def __init__(self, lon: float, lat: float):
        zone = int((lon + 180) // 6) + 1
        crs = f"EPSG:{(32600 if lat >= 0 else 32700) + zone}"
        fwd = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
        inv = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
        self._f = lambda a: np.column_stack(fwd.transform(a[:, 0], a[:, 1]))
        self._i = lambda a: np.column_stack(inv.transform(a[:, 0], a[:, 1]))

    def to_m(self, geom):
        return shapely.transform(geom, self._f)

    def to_ll(self, geom):
        return shapely.transform(geom, self._i)

    def buffer(self, geom, metres: float):
        if metres <= 0:
            return geom
        return self.to_ll(self.to_m(geom).buffer(metres))


@dataclass
class Grid:
    """A window of the global MERIT Hydro grid (origin -180, 90; 3 arc-seconds)."""
    col0: int
    row0: int
    width: int
    height: int

    @property
    def x0(self) -> float:
        return -180.0 + self.col0 * RES

    @property
    def y0(self) -> float:
        return 90.0 - self.row0 * RES

    def centers(self, rows, cols):
        return self.x0 + (np.asarray(cols) + 0.5) * RES, self.y0 - (np.asarray(rows) + 0.5) * RES

    @classmethod
    def for_bounds(cls, minx, miny, maxx, maxy):
        col0 = math.floor((minx + 180.0) * 1200.0)
        col1 = math.ceil((maxx + 180.0) * 1200.0)
        row0 = math.floor((90.0 - maxy) * 1200.0)
        row1 = math.ceil((90.0 - miny) * 1200.0)
        return cls(col0, row0, col1 - col0, row1 - row0)


def chaikin(coords, iterations: int = 2):
    """Corner-cutting smoothing; first and last vertex stay fixed so lines stay connected."""
    for _ in range(iterations):
        if len(coords) < 3:
            break
        new = [coords[0]]
        for (x0, y0), (x1, y1) in zip(coords[:-1], coords[1:]):
            new.append((0.75 * x0 + 0.25 * x1, 0.75 * y0 + 0.25 * y1))
            new.append((0.25 * x0 + 0.75 * x1, 0.25 * y0 + 0.75 * y1))
        new.append(coords[-1])
        coords = new
    return coords


def line_length_m(line: LineString) -> float:
    return float(GEOD.geometry_length(line))


def trim_to_river(line: LineString, river):
    """Cut off the parts of the line before it first enters and after it last leaves the river
    polygon. Returns (trimmed_line, d_start, d_end), d_* measured along the input line."""
    inter = line.intersection(river)
    ds = [line.project(Point(c)) for g in getattr(inter, "geoms", [inter])
          if not g.is_empty and hasattr(g, "coords") for c in g.coords]
    d0, d1 = (min(ds), max(ds)) if ds else (0.0, line.length)
    return (substring(line, d0, d1) if d1 > d0 else line), d0, d1


def river_centreline(river, start: Point, end: Point, proj: LocalProjection):
    """Line from start to end that runs along the middle of the river polygon and never leaves it:
    shortest path over the polygon's medial axis (Voronoi edges of the banks that lie inside).
    Returns None if the polygon gives no connected path."""
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import dijkstra
    from scipy.spatial import Voronoi, cKDTree

    rm = proj.to_m(river)
    spacing = max(0.5, min(5.0, rm.boundary.length / 40000.0))
    bank_pts = np.unique(shapely.get_coordinates(shapely.segmentize(rm.boundary, spacing)), axis=0)
    if len(bank_pts) < 4:
        return None
    vor = Voronoi(bank_pts)  # Qhull: fast for tens of thousands of bank points
    rv = np.array(vor.ridge_vertices)
    rv = rv[(rv >= 0).all(axis=1)]
    a_all, b_all = vor.vertices[rv[:, 0]], vor.vertices[rv[:, 1]]
    inside = rm.buffer(spacing * 0.5)
    shapely.prepare(inside)
    keep = shapely.within(shapely.linestrings(np.stack([a_all, b_all], axis=1)), inside)
    if not keep.any():
        return None
    a, b = a_all[keep], b_all[keep]
    nodes, inv = np.unique(np.round(np.vstack([a, b]), 3), axis=0, return_inverse=True)
    inv = inv.ravel()
    n_e = len(a)
    w = np.hypot(*(a - b).T) + 1e-9
    graph = coo_matrix((w, (inv[:n_e], inv[n_e:])), shape=(len(nodes), len(nodes))).tocsr()
    tree = cKDTree(nodes)
    sm, em = proj.to_m(start), proj.to_m(end)
    si, ei = int(tree.query([sm.x, sm.y])[1]), int(tree.query([em.x, em.y])[1])
    if si == ei:
        return None
    dist, pred = dijkstra(graph, directed=False, indices=si, return_predecessors=True)
    if not np.isfinite(dist[ei]):
        return None
    path, cur = [], ei
    while cur != -9999 and cur != si:
        path.append(cur)
        cur = pred[cur]
    path.append(si)
    line_m = LineString(nodes[path[::-1]]).simplify(spacing)
    line_m = LineString(chaikin(list(line_m.coords), 3))
    return pull_inside(proj.to_ll(line_m), river)


def pull_inside(line: LineString, river, step_m: float = 10.0):
    """Fallback / final touch: move any vertex outside the polygon onto the nearest bank."""
    dense = shapely.segmentize(line, step_m / 111320.0)
    coords = shapely.get_coordinates(dense)
    ok = shapely.covers(river, shapely.points(coords))
    if ok.all():
        return line
    boundary = river.boundary
    out = []
    for (x, y), good in zip(coords, ok):
        if not good:
            q = nearest_points(boundary, Point(x, y))[0]
            x, y = q.x, q.y
        if not out or (x, y) != out[-1]:
            out.append((x, y))
    return LineString(out) if len(out) >= 2 else line


def snap_to_main(junction: Point, main_raw: LineString, d0: float, d1: float, main_line: LineString) -> Point:
    """Where a tributary joins the drawn main stream. Uses its position along the MERIT channel
    so it is not snapped onto a neighbouring loop of a meandering river."""
    frac = (main_raw.project(junction) - d0) / (d1 - d0) if d1 > d0 else 0.0
    frac = min(1.0, max(0.0, frac))
    L = main_line.length
    half = max(0.05 * L, 300.0 / 111320.0)
    window = substring(main_line, max(0.0, frac * L - half), min(L, frac * L + half))
    target = window if window.geom_type == "LineString" and window.length > 0 else main_line
    return nearest_points(target, junction)[0]


# --------------------------------------------------------------------------------------
# MERIT Hydro source (Google Earth Engine)
# --------------------------------------------------------------------------------------
class EarthEngineSource:
    """Fetches MERIT Hydro upa / dir / elv for a Grid through ee.data.computePixels."""

    label = "MERIT Hydro v1.0.1 (Google Earth Engine)"

    def __init__(self, project: str | None = None, service_account: str | None = None,
                 key_file: str | None = None):
        try:
            import ee
        except ImportError:
            raise DrainageError("earthengine-api is not installed: pip install earthengine-api", 503)
        try:
            if key_file:
                creds = ee.ServiceAccountCredentials(service_account, key_file)
                ee.Initialize(creds, project=project)
            else:
                ee.Initialize(project=project)
        except Exception as e:
            raise DrainageError(
                "Earth Engine could not start. Run `earthengine authenticate` once and set "
                "EE_PROJECT to a Google Cloud project registered for Earth Engine "
                f"(or set EE_SERVICE_ACCOUNT + EE_KEY_FILE). Details: {e}", 503)
        self.ee = ee
        # same dataset as in the Code Editor: var merit = ee.Image('MERIT/Hydro/v1_0_1');
        self.image = ee.Image(MERIT_ASSET).select(["upa", "dir", "elv"]).toFloat().unmask(NODATA)

    def fetch(self, grid: Grid, tile: int = 1024):
        out = {b: np.full((grid.height, grid.width), NODATA, np.float32) for b in ("upa", "dir", "elv")}
        for r in range(0, grid.height, tile):
            for c in range(0, grid.width, tile):
                h, w = min(tile, grid.height - r), min(tile, grid.width - c)
                request = {
                    "expression": self.image,
                    "fileFormat": "NUMPY_NDARRAY",
                    "grid": {
                        "dimensions": {"width": w, "height": h},
                        "affineTransform": {
                            "scaleX": RES, "shearX": 0, "translateX": grid.x0 + c * RES,
                            "shearY": 0, "scaleY": -RES, "translateY": grid.y0 - r * RES,
                        },
                        "crsCode": "EPSG:4326",
                    },
                }
                try:
                    arr = self.ee.data.computePixels(request)
                except Exception as e:
                    raise DrainageError(f"Earth Engine request failed: {e}", 502)
                for b in out:
                    out[b][r:r + h, c:c + w] = arr[b]
        return out


# --------------------------------------------------------------------------------------
# Network extraction
# --------------------------------------------------------------------------------------
def extract_network(rasters, grid: Grid, river, river_snap, p: Params):
    upa_r = rasters["upa"].astype(np.float64)
    dir_r = np.rint(rasters["dir"]).astype(np.int64)
    elv_r = rasters["elv"].astype(np.float64)
    H, W = upa_r.shape

    stream = (upa_r > 0) & (upa_r >= p.threshold_km2)
    rr, cc = np.nonzero(stream)
    n = len(rr)
    if n == 0:
        raise DrainageError(
            f"No MERIT stream pixels with upstream area >= {p.threshold_km2} km² in the area. "
            "Lower threshold_km2.")

    ids = np.full((H, W), -1, np.int64)
    ids[rr, cc] = np.arange(n)
    code = dir_r[rr, cc]
    drow, dcol = np.zeros(n, np.int64), np.zeros(n, np.int64)
    has_dir = np.zeros(n, bool)
    for k, (a, b) in D8.items():
        m = code == k
        drow[m], dcol[m], has_dir[m] = a, b, True
    r2, c2 = rr + drow, cc + dcol
    inb = has_dir & (r2 >= 0) & (r2 < H) & (c2 >= 0) & (c2 < W)
    down = np.full(n, -1, np.int64)
    down[inb] = ids[r2[inb], c2[inb]]

    ups = [[] for _ in range(n)]
    for i in np.nonzero(down >= 0)[0]:
        ups[down[i]].append(int(i))

    # topological order, sources first (Kahn)
    indeg = np.bincount(down[down >= 0], minlength=n)
    stack = list(np.nonzero(indeg == 0)[0])
    order = []
    while stack:
        q = int(stack.pop())
        order.append(q)
        d = down[q]
        if d >= 0:
            indeg[d] -= 1
            if indeg[d] == 0:
                stack.append(d)

    upa = upa_r[rr, cc]
    elv = elv_r[rr, cc]
    lon, lat = grid.centers(rr, cc)
    inside_snap = shapely.contains_xy(river_snap, lon, lat)
    on_edge = (rr == 0) | (rr == H - 1) | (cc == 0) | (cc == W - 1)

    # Strahler order (within the AOI)
    strahler = np.zeros(n, np.int64)
    for q in order:
        if not ups[q]:
            strahler[q] = 1
        else:
            vals = [strahler[u] for u in ups[q]]
            m = max(vals)
            strahler[q] = m + 1 if vals.count(m) >= 2 else m

    # ---- main stream ---------------------------------------------------------------
    cand = np.nonzero(inside_snap)[0]
    if len(cand) == 0:
        raise DrainageError(
            f"No MERIT stream (upstream area >= {p.threshold_km2} km²) passes through the river "
            f"polygon (+{p.snap_m:.0f} m). Lower threshold_km2 or increase snap_m.")
    outlet = int(cand[np.argmax(upa[cand])])

    score = np.where(inside_snap, 1.0, -0.5)
    best = np.zeros(n)
    for q in order:
        m = max((best[u] for u in ups[q]), default=0.0)
        best[q] = score[q] + max(0.0, m)

    path = [outlet]
    cur = outlet
    while True:
        kids = [u for u in ups[cur] if best[u] > 0]
        if not kids:
            break
        top = max(best[u] for u in kids)
        close = [u for u in kids if best[u] >= top - 2.0]  # near-tie -> follow the larger river
        cur = max(close, key=lambda u: upa[u])
        path.append(cur)
    in_main = np.zeros(n, bool)
    in_main[path] = True

    # where does every stream pixel drain to? 0 = main stream, 1 = into polygon directly, 2 = elsewhere
    dest = np.full(n, 2, np.int8)
    for q in reversed(order):
        if in_main[q]:
            dest[q] = 0
        elif down[q] >= 0:
            dest[q] = dest[down[q]]
        else:
            dest[q] = 1 if inside_snap[q] else 2

    # ---- group upstream pixels into tributaries -------------------------------------
    queue = deque()
    for q in path:
        kids = sorted((u for u in ups[q] if not in_main[u]), key=lambda u: -upa[u])
        for u in kids:
            queue.append((u, q, "MAIN", 1, 0))
    for q in np.nonzero((down < 0) & (dest == 1))[0]:
        queue.append((int(q), None, "POLYGON", 1, 1))
    if p.include_other:
        for q in np.nonzero((down < 0) & (dest == 2))[0]:
            queue.append((int(q), None, "OUTSIDE", 1, 2))

    headwater_root = None
    top_kids = [u for u in ups[path[-1]] if not in_main[u]]
    if top_kids:
        headwater_root = max(top_kids, key=lambda u: upa[u])

    streams = []
    while queue:
        root, junction, parent, level, dcls = queue.popleft()
        sid = len(streams)
        pix, cur = [root], root
        while True:
            kids = [u for u in ups[cur] if dest[u] == dcls and not in_main[u]]
            if not kids:
                break
            nxt = max(kids, key=lambda u: upa[u])
            for u in kids:
                if u != nxt:
                    queue.append((u, cur, sid, level + 1, dcls))
            pix.append(nxt)
            cur = nxt
        streams.append(dict(sid=sid, pixels=pix, junction=junction, parent=parent,
                            level=level, dest=int(dcls), headwater=(root == headwater_root)))

    return dict(n=n, upa=upa, elv=elv, lon=lon, lat=lat, strahler=strahler, on_edge=on_edge,
                inside_snap=inside_snap, path=path, outlet=outlet, streams=streams)


# --------------------------------------------------------------------------------------
# Build output features
# --------------------------------------------------------------------------------------
def build_features(net, river_name: str, river, p: Params):
    lon, lat, upa, elv = net["lon"], net["lat"], net["upa"], net["elv"]
    xy = lambda i: (float(lon[i]), float(lat[i]))

    # main stream, upstream -> downstream
    main_pix = net["path"][::-1]
    main_coords = [xy(i) for i in main_pix]
    if len(main_coords) == 1:  # one-pixel main stream: extend to its downstream neighbour direction
        x, y = main_coords[0]
        main_coords = [(x - RES / 2, y), (x + RES / 2, y)]
    if p.smooth:
        main_coords = chaikin(main_coords)
    main_raw = LineString(main_coords)  # MERIT channel (90 m), upstream -> downstream
    trimmed, d_start, d_end = trim_to_river(main_raw, river)
    c = river.centroid
    main_line = river_centreline(river, Point(trimmed.coords[0]), Point(trimmed.coords[-1]),
                                 LocalProjection(c.x, c.y))
    if main_line is None:
        main_line = pull_inside(trimmed, river)
    geoms = {"MAIN": main_line}

    def joins_outside(s):
        """True if the stream joins the main stream beyond either end of the river polygon."""
        if s["parent"] != "MAIN" or s["junction"] is None:
            return False
        d = main_raw.project(Point(xy(s["junction"])))
        return d < d_start - RES / 2 or d > d_end + RES / 2

    # tributaries: drop short leaves first
    children = {}
    for s in net["streams"]:
        children.setdefault(s["parent"], []).append(s["sid"])
    features, keep = [], {}
    for s in net["streams"]:
        pix = s["pixels"][::-1]  # source -> mouth
        coords = [xy(i) for i in pix]
        if s["junction"] is not None:
            coords.append(xy(s["junction"]))
        if len(coords) < 2:
            continue
        raw_len = line_length_m(LineString(coords))
        if raw_len < p.min_length_m and s["sid"] not in children:
            continue
        keep[s["sid"]] = (s, coords)

    # number upstreams: level, then larger catchment first
    ordered = sorted(keep.values(), key=lambda t: (t[0]["level"], -upa[t[0]["pixels"][0]]))
    label = {}
    counters = {}
    for s, _ in ordered:
        cls = "upstream" if s["dest"] in (0, 1) else "other"
        counters[cls] = counters.get(cls, 0) + 1
        label[s["sid"]] = f"{'U' if cls == 'upstream' else 'X'}-{counters[cls]:03d}"

    # geometry: process in BFS (sid) order so parents are smoothed before children snap to them
    for sid in sorted(keep):
        s, coords = keep[sid]
        if p.smooth:
            coords = chaikin(coords)
        parent_geom = geoms.get(s["parent"])
        if s["junction"] is not None and s["parent"] == "MAIN":
            end = snap_to_main(Point(xy(s["junction"])), main_raw, d_start, d_end, main_line)
            coords[-1] = (end.x, end.y)
        elif s["junction"] is not None and parent_geom is not None:
            end = parent_geom.interpolate(parent_geom.project(Point(coords[-1])))
            coords[-1] = (end.x, end.y)
        geoms[sid] = LineString(coords)

    main_len = line_length_m(main_line)
    top, out = net["path"][-1], net["outlet"]
    main_props = {
        "class": "main_stream",
        "name": f"{river_name} main stream",
        "length_m": round(main_len, 1),
        "upa_outlet_km2": round(float(upa[out]), 3),
        "upa_top_km2": round(float(upa[top]), 3),
        "elev_top_m": round(float(elv[top]), 1),
        "elev_outlet_m": round(float(elv[out]), 1),
        "slope_pct": round((float(elv[top]) - float(elv[out])) / main_len * 100, 3) if main_len else 0.0,
        "strahler_outlet": int(net["strahler"][out]),
        "merit_channel_inside_pct": round(100.0 * float(net["inside_snap"][net["path"]].mean()), 1),
    }
    features.append({"type": "Feature", "geometry": mapping(main_line), "properties": main_props})

    parent_label = lambda s: ("Main stream" if s["parent"] == "MAIN" else
                              "River polygon (direct)" if s["parent"] == "POLYGON" else
                              "Drains outside the river" if s["parent"] == "OUTSIDE" else
                              label.get(s["parent"], "removed"))
    for s, _ in ordered:
        line = geoms[s["sid"]]
        mouth, source = s["pixels"][0], s["pixels"][-1]
        end_elev = float(elv[s["junction"]]) if s["junction"] is not None else float(elv[mouth])
        length = line_length_m(line)
        cls = "upstream" if s["dest"] in (0, 1) else "other"
        props = {
            "class": cls,
            "name": label[s["sid"]],
            "level": s["level"],
            "drains_into": parent_label(s),
            "role": "main-stem headwater (river continues upstream of the polygon)" if s["headwater"]
                    else "tributary",
            "length_m": round(length, 1),
            "upa_mouth_km2": round(float(upa[mouth]), 3),
            "upa_source_km2": round(float(upa[source]), 3),
            "strahler": int(net["strahler"][mouth]),
            "elev_source_m": round(float(elv[source]), 1),
            "elev_mouth_m": round(end_elev, 1),
            "slope_pct": round((float(elv[source]) - end_elev) / length * 100, 3) if length else 0.0,
            "cut_by_aoi_edge": bool(net["on_edge"][source]),
            "outside_river": joins_outside(s),
        }
        features.append({"type": "Feature", "geometry": mapping(line), "properties": props})

    # points: outlet + level-1 confluences on the main stream
    end = main_line.coords[-1]
    features.append({"type": "Feature", "geometry": mapping(Point(end)),
                     "properties": {"class": "outlet", "name": "Outlet (most downstream point in polygon)",
                                    "upa_km2": main_props["upa_outlet_km2"]}})
    for s, _ in ordered:
        if s["level"] == 1 and s["parent"] == "MAIN":
            c = geoms[s["sid"]].coords[-1]
            features.append({"type": "Feature", "geometry": mapping(Point(c)),
                             "properties": {"class": "confluence", "name": f"Confluence {label[s['sid']]}",
                                            "upa_km2": round(float(upa[s['pixels'][0]]), 3)}})
    return features


# --------------------------------------------------------------------------------------
# KML writer
# --------------------------------------------------------------------------------------
# --------------------------------------------------------------------------------------
# Combine branches: one feature per upstream that joins the river
# --------------------------------------------------------------------------------------
UPSTREAM_COLORS = ["#d7263d", "#e07a10", "#8e44ad", "#c2185b", "#2e7d32",
                   "#00838f", "#6d4c41", "#b8860b", "#5c6bc0", "#7cb342"]


def combine_upstreams(features):
    """Return (main_feature, systems, removed). Each system = one upstream that joins the river
    from the side (into the main stream or straight into the polygon) with every branch that feeds it.
    Drainage outside the river (beyond its ends) is removed."""
    main = next(f for f in features if f["properties"]["class"] == "main_stream")
    main_line = shape(main["geometry"])
    ups = {f["properties"]["name"]: f for f in features if f["properties"]["class"] == "upstream"}

    def root_of(name):
        seen = set()
        while ups[name]["properties"]["drains_into"] in ups and name not in seen:
            seen.add(name)
            name = ups[name]["properties"]["drains_into"]
        return name

    groups = {}
    for name in ups:  # level order, so each group's root comes first
        groups.setdefault(root_of(name), []).append(ups[name])

    systems, removed = [], 0
    for root, members in groups.items():
        rp = ups[root]["properties"]
        # not part of this river: the river continuing above the polygon (headwater network)
        # or anything that joins beyond either end of the polygon
        if "headwater" in rp["role"] or rp["outside_river"]:
            removed += 1
            continue
        lines = [shape(m["geometry"]) for m in members]
        mouth = Point(lines[0].coords[-1])
        d = main_line.project(mouth)
        chainage = line_length_m(substring(main_line, 0, d)) if d > 0 else 0.0
        props = {
            "joins": rp["drains_into"],
            "chainage_km": round(chainage / 1000, 3),
            "confluence_lon": round(mouth.x, 6),
            "confluence_lat": round(mouth.y, 6),
            "upa_at_confluence_km2": rp["upa_mouth_km2"],
            "branches": len(members),
            "main_branch_length_km": round(rp["length_m"] / 1000, 3),
            "total_length_km": round(sum(m["properties"]["length_m"] for m in members) / 1000, 3),
            "strahler": rp["strahler"],
            "elev_highest_source_m": max(m["properties"]["elev_source_m"] for m in members),
            "elev_confluence_m": rp["elev_mouth_m"],
            "cut_by_aoi_edge": any(m["properties"]["cut_by_aoi_edge"] for m in members),
        }
        systems.append({"type": "Feature", "geometry": mapping(MultiLineString(lines)), "properties": props})

    systems.sort(key=lambda s: s["properties"]["chainage_km"])  # numbered from upstream end of the river
    for i, s in enumerate(systems, 1):
        s["properties"] = {"name": f"Upstream {i:02d}", **s["properties"]}
    main["properties"]["upstreams_joining"] = len(systems)
    return main, systems, removed


# --------------------------------------------------------------------------------------
# KML writers
# --------------------------------------------------------------------------------------
def _kml_color(hex_rgb: str, alpha: str = "ff") -> str:
    h = hex_rgb.lstrip("#")
    return f"{alpha}{h[4:6]}{h[2:4]}{h[0:2]}"


def _coords_txt(coords):
    return " ".join(f"{x:.7f},{y:.7f},0" for x, y in coords)


def _ext(props):
    rows = "".join(f'<Data name="{escape(str(k))}"><value>{escape(str(v))}</value></Data>'
                   for k, v in props.items())
    return f"<ExtendedData>{rows}</ExtendedData>"


def _table(props):
    rows = "".join(f"<tr><td>{escape(str(k))}</td><td><b>{escape(str(v))}</b></td></tr>"
                   for k, v in props.items() if k != "name" and not isinstance(v, (dict, list)))
    return f"<description><![CDATA[<table>{rows}</table>]]></description>"


def _linestring(coords):
    return f"<LineString><tessellate>1</tessellate><coordinates>{_coords_txt(coords)}</coordinates></LineString>"


def _placemark(feature, style):
    props, g = feature["properties"], feature["geometry"]
    if g["type"] == "LineString":
        geom = _linestring(g["coordinates"])
    elif g["type"] == "MultiLineString":
        geom = "<MultiGeometry>" + "".join(_linestring(c) for c in g["coordinates"]) + "</MultiGeometry>"
    else:
        raise ValueError(g["type"])
    return (f"<Placemark><name>{escape(props['name'])}</name>{_table(props)}"
            f"<styleUrl>#{style}</styleUrl>{_ext(props)}{geom}</Placemark>")


def _kml_document(title, summary, styles, placemarks):
    return "\n".join([
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<kml xmlns="http://www.opengis.net/kml/2.2"><Document>',
        f"<name>{escape(title)}</name>", _table(summary), styles, *placemarks,
        "</Document></kml>"])


def to_main_stream_kml(main, river_name, summary):
    style = '<Style id="main"><LineStyle><color>ffd84e1d</color><width>5</width></LineStyle></Style>'
    return _kml_document(f"{river_name}: main stream", summary, style, [_placemark(main, "main")])


def to_upstreams_kml(systems, river_name, summary):
    styles = "".join(f'<Style id="u{i}"><LineStyle><color>{_kml_color(c)}</color><width>2.5</width></LineStyle></Style>'
                     for i, c in enumerate(UPSTREAM_COLORS))
    pms = [_placemark(s, f"u{i % len(UPSTREAM_COLORS)}") for i, s in enumerate(systems)]
    return _kml_document(f"{river_name}: upstreams ({len(systems)})", summary, styles, pms)


# --------------------------------------------------------------------------------------
# Pipeline
# --------------------------------------------------------------------------------------
@dataclass
class Result:
    river_name: str
    main_kml: str
    upstreams_kml: str
    geojson: dict
    summary: dict


def run_pipeline(data: bytes, filename: str, params: Params, source_factory) -> Result:
    """source_factory(river_geom) -> object with .label and .fetch(Grid) -> {'upa','dir','elv'}"""
    params.validate()
    river_name, river = read_river_kml(data, filename)
    if params.river_name:
        river_name = params.river_name.strip() or river_name

    c = river.centroid
    proj = LocalProjection(c.x, c.y)
    river_snap = proj.buffer(river, params.snap_m)
    aoi = proj.buffer(shapely.box(*river.bounds), params.buffer_m)
    grid = Grid.for_bounds(*aoi.bounds)
    if grid.width * grid.height > MAX_AOI_PIXELS:
        raise DrainageError("The area is too large for one request. Split the polygon into parts.", 413)

    source = source_factory(river)
    rasters = source.fetch(grid)
    net = extract_network(rasters, grid, river, river_snap, params)
    features = build_features(net, river_name, river, params)
    main, systems, removed = combine_upstreams(features)

    mp = main["properties"]
    summary = {
        "river": river_name,
        "data_source": source.label,
        "processed_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
        "stream_threshold_km2": params.threshold_km2,
        "search_buffer_m": params.buffer_m,
        "main_stream_length_km": round(mp["length_m"] / 1000, 2),
        "main_stream_upa_outlet_km2": mp["upa_outlet_km2"],
        "upstreams": len(systems),
        "upstream_branches": sum(s["properties"]["branches"] for s in systems),
        "upstreams_total_length_km": round(sum(s["properties"]["total_length_km"] for s in systems), 2),
        "removed_outside_river": removed,
    }
    river_feature = {"type": "Feature", "geometry": mapping(river),
                     "properties": {"class": "river_polygon", "name": river_name}}
    sys_features = [{**s, "properties": {**s["properties"], "class": "upstream",
                                         "color": UPSTREAM_COLORS[i % len(UPSTREAM_COLORS)]}}
                    for i, s in enumerate(systems)]
    geojson = {"type": "FeatureCollection", "features": [river_feature, main, *sys_features]}
    return Result(river_name, to_main_stream_kml(main, river_name, summary),
                  to_upstreams_kml(systems, river_name, summary), geojson, summary)


def safe_filename(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_").lower() or "river"
