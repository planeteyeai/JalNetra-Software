"""
jalnetra.kml_locator — parse uploaded KML and build river reach / chainage.

Chainage and reach names come from Point placemarks (bridges) in the KML when
present. Each uploaded river gets its own synthetic demo seed from the centroid
so responses differ by KML (e.g. Godavari ≠ Mithi).
"""
from __future__ import annotations

import hashlib
import math
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple

from .config import Reach


@dataclass
class RiverProfile:
    key: str
    name: str
    reaches_fn: Callable[[], List[Reach]]
    # (west, south, east, north) approximate bounding box
    bbox: Tuple[float, float, float, float]
    # river axis: upstream (lon, lat) → downstream (lon, lat)
    axis_start: Tuple[float, float]
    axis_end: Tuple[float, float]
    total_km: float
    demo_seed: int
    holdout_reach: str
    rain_seed: int
    landmarks: Optional[List[Tuple[str, float]]] = None


def _local_name(tag: str) -> str:
    return tag.split("}")[-1] if "}" in tag else tag


def _parse_coordinate_rings(kml_bytes: bytes) -> List[List[List[float]]]:
    try:
        root = ET.fromstring(kml_bytes)
    except ET.ParseError as exc:
        raise ValueError(f"Invalid KML file: {exc}") from exc

    rings: List[List[List[float]]] = []
    for elem in root.iter():
        if _local_name(elem.tag) != "coordinates":
            continue
        if not elem.text or not elem.text.strip():
            continue
        tokens = elem.text.strip().split()
        ring: List[List[float]] = []
        for token in tokens:
            parts = token.split(",")
            if len(parts) >= 2:
                ring.append([float(parts[0]), float(parts[1])])
        # Polygon / LineString rings need ≥2 points; closed polygons ≥3
        if len(ring) >= 3:
            if ring[0] != ring[-1]:
                ring.append(ring[0])
            rings.append(ring)
        elif len(ring) == 2:
            rings.append(ring)
    return rings


def _parse_document_name(root: ET.Element) -> Optional[str]:
    for elem in root.iter():
        if _local_name(elem.tag) not in ("Document", "Folder"):
            continue
        for child in list(elem):
            if _local_name(child.tag) == "name" and child.text and child.text.strip():
                return child.text.strip()
    return None


# Common India river names (substring match against KML text)
_KNOWN_RIVERS = (
    "Godavari",
    "Ganga",
    "Ganges",
    "Yamuna",
    "Narmada",
    "Tapi",
    "Tapti",
    "Krishna",
    "Kaveri",
    "Cauvery",
    "Mahanadi",
    "Brahmaputra",
    "Indus",
    "Sutlej",
    "Beas",
    "Ravi",
    "Chenab",
    "Jhelum",
    "Chambal",
    "Betwa",
    "Son",
    "Ghaghara",
    "Gandak",
    "Kosi",
    "Damodar",
    "Hooghly",
    "Sabarmati",
    "Mahi",
    "Luni",
    "Periyar",
    "Pennar",
    "Tungabhadra",
    "Bhima",
    "Wardha",
    "Wainganga",
    "Indravati",
    "Pranhita",
    "Manjira",
    "Purna",
    "Mula",
    "Mutha",
    "Mula-Mutha",
    "Mula–Mutha",
    "Mithi",
    "Ulhas",
    "Patalganga",
    "Amba",
    "Savitri",
    "Vashishti",
    "Koyna",
    "Ghod",
    "Pavna",
    "Indrayani",
    "Bhima",
    "Nira",
    "Kundalika",
    "Zuari",
    "Mandovi",
    "Sharavathi",
    "Netravati",
    "Palar",
    "Vaigai",
    "Tamiraparani",
    "Bhavani",
    "Amaravati",
    "Musí",
    "Musi",
    "Gomati",
    "Gomti",
    "Rapti",
    "Ken",
    "Tons",
    "Shipra",
    "Kshipra",
    "Saraswati",
    "Sabarmati",
)

_GENERIC_NAME_PARTS = (
    "untitled",
    "placemark",
    "document",
    "folder",
    "kml",
    "aoi",
    "stretch",
    "polygon",
    "linestring",
    "path",
    "layer",
    "export",
    "boundary",
    "buffer",
    "temp",
    "test",
    "new",
    "copy",
    "drawing",
    "my places",
)


def _clean_label(text: str) -> str:
    s = " ".join((text or "").replace("_", " ").replace("-", " ").split())
    return s.strip(" -_|.,;:/\\")


def _is_generic_label(text: str) -> bool:
    t = (text or "").strip().lower()
    if not t or len(t) < 2:
        return True
    if t.startswith("b") and t[1:].isdigit():
        return True  # B1, B2…
    if t.startswith("r") and t[1:].isdigit():
        return True
    return any(g in t for g in _GENERIC_NAME_PARTS)


def _looks_like_river_phrase(text: str) -> bool:
    t = (text or "").lower()
    return any(
        k in t
        for k in (
            "river",
            "nadi",
            "nadii",
            "nadhi",
            "stream",
            "creek",
            "tributary",
            "basin",
        )
    )


def _extract_river_from_phrase(text: str) -> Optional[str]:
    """Pull a river name out of phrases like 'Godavari River AOI'."""
    s = _clean_label(text)
    if not s:
        return None
    # Known-name hit first (longest match)
    lower = s.lower()
    best = None
    for name in sorted(_KNOWN_RIVERS, key=len, reverse=True):
        if name.lower() in lower:
            best = name
            break
    if best:
        # Prefer "Mula–Mutha" spelling
        if best.lower() in ("mula-mutha", "mula–mutha"):
            return "Mula–Mutha"
        if best.lower() == "ganges":
            return "Ganga"
        if best.lower() == "cauvery":
            return "Kaveri"
        if best.lower() == "tapti":
            return "Tapi"
        return best

    patterns = [
        r"(?i)\b(?:river|nadi|nadhi)\s+([A-Za-z][A-Za-z\s]{1,40})",
        r"(?i)\b([A-Za-z][A-Za-z\s]{1,40})\s+(?:river|nadi|nadhi)\b",
    ]
    for pat in patterns:
        m = re.search(pat, s)
        if m:
            cand = _clean_label(m.group(1))
            # Trim trailing junk words
            for junk in ("aoi", "kml", "stretch", "basin", "corridor", "reach"):
                if cand.lower().endswith(" " + junk):
                    cand = cand[: -(len(junk) + 1)].strip()
            if cand and not _is_generic_label(cand):
                return cand.title() if cand.islower() or cand.isupper() else cand
    return None


def _collect_kml_labels(root: ET.Element) -> List[str]:
    labels: List[str] = []
    for elem in root.iter():
        tag = _local_name(elem.tag)
        if tag in ("name", "description", "Snippet", "value") and elem.text:
            t = elem.text.strip()
            if t:
                labels.append(t)
    return labels


def _guess_river_name(root: ET.Element) -> str:
    """
    Guess river name from KML only (Document/Folder/placemark names & text).
    Used for the BOD/COD dashboard title.
    """
    labels = _collect_kml_labels(root)
    doc_name = _parse_document_name(root)

    def _normalize_known(name: str) -> str:
        low = name.lower()
        if low == "ganges":
            return "Ganga"
        if low == "cauvery":
            return "Kaveri"
        if low in ("mula-mutha", "mula–mutha", "mula mutha"):
            return "Mula–Mutha"
        if low == "tapti":
            return "Tapi"
        return name

    # 1) Known river name anywhere in KML text (longest match)
    blob = " | ".join(labels)
    lower_blob = blob.lower()
    for name in sorted(_KNOWN_RIVERS, key=len, reverse=True):
        if name.lower() in lower_blob:
            return _normalize_known(name)

    # 2) Phrases like "X River" / "River X"
    for label in labels:
        extracted = _extract_river_from_phrase(label)
        if extracted:
            return _normalize_known(extracted)

    # 3) Document / folder name if usable
    if doc_name:
        extracted = _extract_river_from_phrase(doc_name)
        if extracted:
            return _normalize_known(extracted)
        cleaned = _clean_label(doc_name)
        if cleaned and not _is_generic_label(cleaned):
            return cleaned

    # 4) First decent placemark / folder label
    for label in labels:
        cleaned = _clean_label(label)
        if not cleaned or _is_generic_label(cleaned) or len(cleaned) > 60:
            continue
        extracted = _extract_river_from_phrase(cleaned)
        if extracted:
            return _normalize_known(extracted)
        if cleaned[0].isalpha():
            return cleaned

    return "Uploaded river"


def _parse_bridge_points(root: ET.Element) -> List[Tuple[str, float, float]]:
    """
    Bridge candidates from KML placemarks:
      - any Point geometry
      - LineString / Polygon midpoints when name suggests a bridge/crossing
    """
    bridges: List[Tuple[str, float, float]] = []
    bridge_keywords = (
        "bridge",
        "crossing",
        "culvert",
        "bandhara",
        "barrage",
        "weir",
        "causeway",
        "flyover",
        "pul",
    )

    for pm in root.iter():
        if _local_name(pm.tag) != "Placemark":
            continue

        name = None
        for child in list(pm):
            if _local_name(child.tag) == "name" and child.text and child.text.strip():
                name = child.text.strip()
                break
        name_l = (name or "").lower()

        # --- Point geometries (including under MultiGeometry) ---
        found_point = False
        for pt in pm.iter():
            if _local_name(pt.tag) != "Point":
                continue
            for coords_elem in pt.iter():
                if _local_name(coords_elem.tag) != "coordinates":
                    continue
                if not coords_elem.text or not coords_elem.text.strip():
                    continue
                token = coords_elem.text.strip().split()[0]
                parts = token.split(",")
                if len(parts) >= 2:
                    bridges.append(
                        (
                            name or f"Bridge {len(bridges) + 1}",
                            float(parts[0]),
                            float(parts[1]),
                        )
                    )
                    found_point = True
                    break
            if found_point:
                break
        if found_point:
            continue

        # --- Line/poly placemarks that look like bridges: use midpoint ---
        if not any(k in name_l for k in bridge_keywords):
            continue
        for coords_elem in pm.iter():
            if _local_name(coords_elem.tag) != "coordinates":
                continue
            if not coords_elem.text or not coords_elem.text.strip():
                continue
            pts: List[Tuple[float, float]] = []
            for token in coords_elem.text.strip().split():
                parts = token.split(",")
                if len(parts) >= 2:
                    pts.append((float(parts[0]), float(parts[1])))
            if len(pts) >= 2:
                mid = pts[len(pts) // 2]
                bridges.append((name or f"Bridge {len(bridges) + 1}", mid[0], mid[1]))
                break

    return bridges


def _sample_bridges_along_axis(
    axis_start: Tuple[float, float],
    axis_end: Tuple[float, float],
    total_km: float,
    *,
    count: Optional[int] = None,
) -> List[Tuple[str, float, float]]:
    """Place B1..Bn evenly along the KML river axis when no point bridges exist."""
    n = count
    if n is None:
        n = max(3, min(8, int(round(total_km / 2.0)) + 1))
    n = max(2, n)
    out: List[Tuple[str, float, float]] = []
    for i in range(n):
        t = i / (n - 1)
        lon = axis_start[0] + t * (axis_end[0] - axis_start[0])
        lat = axis_start[1] + t * (axis_end[1] - axis_start[1])
        out.append((f"B{i + 1}", lon, lat))
    return out


def _haversine_km(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    )
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def _axis_from_points(
    points: List[Tuple[float, float]],
) -> Tuple[Tuple[float, float], Tuple[float, float], float]:
    """Longest-span axis through point cloud (lon, lat)."""
    if not points:
        raise ValueError("No coordinates found in KML.")
    if len(points) == 1:
        lon, lat = points[0]
        return (lon, lat), (lon + 0.01, lat), 1.0

    best_i, best_j, best_d = 0, 1, -1.0
    for i in range(len(points)):
        for j in range(i + 1, len(points)):
            d = _haversine_km(points[i][0], points[i][1], points[j][0], points[j][1])
            if d > best_d:
                best_d = d
                best_i, best_j = i, j
    start = points[best_i]
    end = points[best_j]
    # Prefer south/west → north/east as "upstream" heuristic
    if (start[1], start[0]) > (end[1], end[0]):
        start, end = end, start
    total = max(best_d, 0.5)
    return start, end, round(total, 2)


def _project_km_on_axis(
    lon: float,
    lat: float,
    start: Tuple[float, float],
    end: Tuple[float, float],
    total_km: float,
) -> float:
    """Project (lon, lat) onto river axis; return chainage in km."""
    x0, y0 = start
    x1, y1 = end
    dx, dy = x1 - x0, y1 - y0
    seg_len2 = dx * dx + dy * dy
    if seg_len2 < 1e-12:
        return 0.0
    t = max(0.0, min(1.0, ((lon - x0) * dx + (lat - y0) * dy) / seg_len2))
    return round(t * total_km, 2)


def _seed_from_centroid(lon: float, lat: float) -> int:
    raw = f"{round(lon, 4)}:{round(lat, 4)}".encode("utf-8")
    return int(hashlib.md5(raw).hexdigest()[:8], 16) % 90000 + 1000


def _build_reaches_from_bridges(
    bridges: List[Tuple[str, float, float]],
    axis_start: Tuple[float, float],
    axis_end: Tuple[float, float],
    total_km: float,
) -> Tuple[List[Reach], List[Tuple[str, float]]]:
    """Order bridges along axis; label them B1, B2, B3… for chainage / reaches."""
    ordered = sorted(
        bridges,
        key=lambda b: _project_km_on_axis(b[1], b[2], axis_start, axis_end, total_km),
    )
    # Sequential bridge IDs for chainage (ignore original placemark names)
    ordered = [
        (f"B{i + 1}", lon, lat) for i, (_name, lon, lat) in enumerate(ordered)
    ]

    # Always keep ≥2 bridges so reach catalogue is B1, B2… (never Upstream/Downstream)
    if len(ordered) == 0:
        ordered = [
            (f"B{i + 1}", lon, lat)
            for i, (_n, lon, lat) in enumerate(
                _sample_bridges_along_axis(axis_start, axis_end, total_km, count=3)
            )
        ]
    elif len(ordered) == 1:
        # Add B2 at the downstream end of the axis
        ordered.append(("B2", axis_end[0], axis_end[1]))

    landmarks: List[Tuple[str, float]] = []
    for name, lon, lat in ordered:
        km = _project_km_on_axis(lon, lat, axis_start, axis_end, total_km)
        landmarks.append((name, km))

    # Reaches span consecutive bridge chainages; first from 0 / last to end
    reaches: List[Reach] = []
    prev: Optional[str] = None
    kms = [lm[1] for lm in landmarks]
    # Ensure increasing chainage for bounds
    if kms[-1] < total_km:
        # extend last landmark span to full length
        pass
    bounds = [0.0] + [(kms[i] + kms[i + 1]) / 2.0 for i in range(len(kms) - 1)] + [
        max(total_km, kms[-1] + 0.5)
    ]
    for i, (name, _lon, _lat) in enumerate(ordered):
        rid = f"R{i + 1:02d}"
        a, b = round(bounds[i], 2), round(bounds[i + 1], 2)
        if b <= a:
            b = a + 0.5
        od = 0.35 + 0.5 * (i / max(1, len(ordered) - 1))
        r = Reach(rid, name, a, b, 70.0 + 5 * i, round(od, 2), upstream=prev)
        r.villages = name  # type: ignore[attr-defined]
        reaches.append(r)
        prev = rid
    return reaches, landmarks


def kml_centroid(kml_bytes: bytes) -> Tuple[float, float]:
    """Return (lon, lat) centroid of all polygon/line coordinates in the KML."""
    rings = _parse_coordinate_rings(kml_bytes)
    xs, ys = [], []
    for ring in rings:
        for lon, lat in ring:
            xs.append(lon)
            ys.append(lat)
    if not xs:
        # Fall back to bridge points only
        try:
            root = ET.fromstring(kml_bytes)
        except ET.ParseError as exc:
            raise ValueError(f"Invalid KML file: {exc}") from exc
        bridges = _parse_bridge_points(root)
        if not bridges:
            raise ValueError("No polygon or point coordinates found in KML.")
        xs = [b[1] for b in bridges]
        ys = [b[2] for b in bridges]
    return sum(xs) / len(xs), sum(ys) / len(ys)


@dataclass
class KmlLocation:
    river_key: str
    river_name: str
    reach_id: str
    reach_name: str
    chainage_km: float
    km_range: Tuple[float, float]
    area_name: str
    villages: Optional[str]
    centroid: Tuple[float, float]
    profile: RiverProfile
    bridges: Optional[List[Tuple[str, float]]] = None


def locate_kml(kml_bytes: bytes) -> KmlLocation:
    """
    Build river profile from uploaded KML.

    Uses river name guessed from KML text (Document/Folder/placemark names).
    Point placemarks are bridges for chainage, labeled B1, B2, B3… along the axis.
    Synthetic demo seed is unique per centroid.
    """
    try:
        root = ET.fromstring(kml_bytes)
    except ET.ParseError as exc:
        raise ValueError(f"Invalid KML file: {exc}") from exc

    lon, lat = kml_centroid(kml_bytes)
    river_name = _guess_river_name(root)
    bridges = _parse_bridge_points(root)

    rings = _parse_coordinate_rings(kml_bytes)
    axis_pts: List[Tuple[float, float]] = []
    for ring in rings:
        for x, y in ring:
            axis_pts.append((x, y))
    for _n, x, y in bridges:
        axis_pts.append((x, y))
    if not axis_pts:
        axis_pts = [(lon, lat)]

    axis_start, axis_end, total_km = _axis_from_points(axis_pts)
    if bridges:
        # Recompute axis from ordered bridges when available
        ordered = sorted(
            bridges,
            key=lambda b: _project_km_on_axis(
                b[1], b[2], axis_start, axis_end, total_km
            ),
        )
        axis_start = (ordered[0][1], ordered[0][2])
        axis_end = (ordered[-1][1], ordered[-1][2])
        total_km = max(
            _haversine_km(axis_start[0], axis_start[1], axis_end[0], axis_end[1]),
            0.5,
        )
        total_km = round(total_km, 2)
    elif not bridges:
        # No point bridges in KML — place B1..Bn along the river corridor axis
        bridges = _sample_bridges_along_axis(axis_start, axis_end, total_km)

    reaches, landmarks = _build_reaches_from_bridges(
        bridges, axis_start, axis_end, total_km
    )
    seed = _seed_from_centroid(lon, lat)
    holdout = reaches[len(reaches) // 2].reach_id

    def _reaches_fn() -> List[Reach]:
        return list(reaches)

    west = min(p[0] for p in axis_pts) - 0.05
    south = min(p[1] for p in axis_pts) - 0.05
    east = max(p[0] for p in axis_pts) + 0.05
    north = max(p[1] for p in axis_pts) + 0.05

    profile = RiverProfile(
        key="custom",
        name=river_name,
        reaches_fn=_reaches_fn,
        bbox=(west, south, east, north),
        axis_start=axis_start,
        axis_end=axis_end,
        total_km=total_km,
        demo_seed=seed,
        holdout_reach=holdout,
        rain_seed=seed + 7,
        landmarks=landmarks,
    )

    chainage = _project_km_on_axis(lon, lat, axis_start, axis_end, total_km)
    reach = next(
        (r for r in reaches if r.km_start <= chainage <= r.km_end),
        min(
            reaches,
            key=lambda r: min(abs(chainage - r.km_start), abs(chainage - r.km_end)),
        ),
    )
    villages = getattr(reach, "villages", None)
    area_name = reach.name
    if villages:
        area_name = f"{reach.name} ({villages})"

    return KmlLocation(
        river_key=profile.key,
        river_name=profile.name,
        reach_id=reach.reach_id,
        reach_name=reach.name,
        chainage_km=chainage,
        km_range=(reach.km_start, reach.km_end),
        area_name=area_name,
        villages=villages,
        centroid=(round(lon, 6), round(lat, 6)),
        profile=profile,
        bridges=landmarks,
    )
