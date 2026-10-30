"""
synthetic.py: OFFLINE TEST SOURCE ONLY (fake terrain, not real data).

Builds a MERIT-like upa / dir / elv stack shaped around the river polygon so the pipeline,
API and map can be tested without Earth Engine. Enable with DRAINAGE_SOURCE=synthetic.
"""
import heapq
from collections import deque

import numpy as np
import shapely
from scipy.ndimage import distance_transform_edt, gaussian_filter, label

from jalnetra.drainage_core import D8, D8_CODE, RES, LocalProjection


class SyntheticSource:
    label = "SYNTHETIC TEST TERRAIN (not real data)"

    def __init__(self, river, seed: int = 7):
        self.river = river
        self.seed = seed

    def fetch(self, grid):
        H, W = grid.height, grid.width
        rows, cols = np.mgrid[0:H, 0:W]
        lon, lat = grid.centers(rows, cols)
        c = self.river.centroid
        wide = LocalProjection(c.x, c.y).buffer(self.river, 60)
        inside = shapely.contains_xy(wide, lon, lat)
        lab, nlab = label(inside, structure=np.ones((3, 3)))
        if nlab > 1:
            sizes = np.bincount(lab.ravel())[1:]
            inside = lab == (np.argmax(sizes) + 1)

        def bfs(start):
            dist = np.full((H, W), -1, np.int64)
            dist[start] = 0
            dq = deque([start])
            while dq:
                r, cc = dq.popleft()
                for dr, dc in D8.values():
                    a, b = r + dr, cc + dc
                    if 0 <= a < H and 0 <= b < W and inside[a, b] and dist[a, b] < 0:
                        dist[a, b] = dist[r, cc] + 1
                        dq.append((a, b))
            return dist

        any_px = tuple(np.argwhere(inside)[0])
        d0 = bfs(any_px)
        end_a = np.unravel_index(np.argmax(d0), d0.shape)
        da = bfs(end_a)
        end_b = np.unravel_index(np.argmax(da), da.shape)
        width = distance_transform_edt(inside)
        outlet = end_a if width[end_a] >= width[end_b] else end_b  # wider end = mouth
        along = bfs(outlet).astype(float)

        px_m = RES * 111320.0
        river_elev = np.where(inside, 2.0 + 0.0015 * along * px_m, np.nan)
        dist_px, (ir, ic) = distance_transform_edt(~inside, return_indices=True)
        rng = np.random.default_rng(self.seed)
        noise = gaussian_filter(rng.standard_normal((H, W)), 4) * 60
        noise += gaussian_filter(rng.standard_normal((H, W)), 1.5) * 4
        dist_m = dist_px * px_m
        elev = river_elev[ir, ic] + 0.025 * dist_m + noise * np.clip(dist_m / 400, 0, 1)
        elev[inside] = river_elev[inside]
        # the real river continues past the AOI: carve an exit channel to the nearest edge
        r0, c0 = outlet
        exits = {(-1, 0): r0, (1, 0): H - 1 - r0, (0, -1): c0, (0, 1): W - 1 - c0}
        (dr, dc) = min(exits, key=exits.get)
        r, cc_, k = r0, c0, 0
        while 0 <= r < H and 0 <= cc_ < W:
            elev[r, cc_] = min(elev[r, cc_], 2.0 - 0.0015 * k * px_m)
            r, cc_, k = r + dr, cc_ + dc, k + 1

        filled = self._priority_flood(elev)
        dirr = self._d8(filled)
        area_km2 = (px_m / 1000) ** 2 * np.cos(np.radians(lat))
        upa = self._accumulate(filled, dirr, area_km2)
        return {"upa": upa.astype(np.float32), "dir": dirr.astype(np.float32),
                "elv": filled.astype(np.float32)}

    @staticmethod
    def _priority_flood(dem, eps=1e-3):
        H, W = dem.shape
        out = dem.copy()
        closed = np.zeros((H, W), bool)
        heap = []
        for r in range(H):
            for c in (0, W - 1):
                if not closed[r, c]:
                    closed[r, c] = True
                    heapq.heappush(heap, (out[r, c], r, c))
        for c in range(W):
            for r in (0, H - 1):
                if not closed[r, c]:
                    closed[r, c] = True
                    heapq.heappush(heap, (out[r, c], r, c))
        while heap:
            z, r, c = heapq.heappop(heap)
            for dr, dc in D8.values():
                a, b = r + dr, c + dc
                if 0 <= a < H and 0 <= b < W and not closed[a, b]:
                    closed[a, b] = True
                    out[a, b] = max(out[a, b], z + eps)
                    heapq.heappush(heap, (out[a, b], a, b))
        return out

    @staticmethod
    def _d8(z):
        H, W = z.shape
        pad = np.pad(z, 1, constant_values=np.inf)
        best = np.zeros((H, W))
        code = np.zeros((H, W), np.int64)
        for (dr, dc), k in D8_CODE.items():
            nb = pad[1 + dr:1 + dr + H, 1 + dc:1 + dc + W]
            drop = (z - nb) / (1.4142 if dr and dc else 1.0)
            m = drop > best
            best[m], code[m] = drop[m], k
        # edge cells with no lower neighbour drain out of the window
        none = code == 0
        code[none & (np.arange(H)[:, None] == 0)] = 64
        code[none & (np.arange(H)[:, None] == H - 1)] = 4
        code[none & (np.arange(W)[None, :] == 0) & (code == 0)] = 16
        code[none & (np.arange(W)[None, :] == W - 1) & (code == 0)] = 1
        return code

    @staticmethod
    def _accumulate(z, code, area):
        H, W = z.shape
        acc = area.astype(float).copy()
        for flat in np.argsort(-z, axis=None):
            r, c = divmod(int(flat), W)
            k = code[r, c]
            if k in D8:
                dr, dc = D8[k]
                a, b = r + dr, c + dc
                if 0 <= a < H and 0 <= b < W:
                    acc[a, b] += acc[r, c]
        return acc
