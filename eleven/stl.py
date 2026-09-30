
from __future__ import annotations

import hashlib
import math
import struct
from collections import Counter
from pathlib import Path

from .models import Mesh, Point3, Triangle


class STLReadError(ValueError):
    pass


def _finite(point: Point3) -> bool:
    return all(math.isfinite(v) for v in point)


def _valid_triangle(tri: Triangle) -> bool:
    if not all(_finite(p) for p in tri):
        return False
    a, b, c = tri
    cross = (
        (b[1] - a[1]) * (c[2] - a[2]) - (b[2] - a[2]) * (c[1] - a[1]),
        (b[2] - a[2]) * (c[0] - a[0]) - (b[0] - a[0]) * (c[2] - a[2]),
        (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0]),
    )
    return sum(v * v for v in cross) > 1e-24


def _parse_binary(data: bytes) -> list[Triangle]:
    if len(data) < 84:
        raise STLReadError("Binary STL is too short.")
    count = struct.unpack_from("<I", data, 80)[0]
    expected = 84 + count * 50
    if expected != len(data):
        raise STLReadError("Binary STL size does not match its face count.")
    triangles: list[Triangle] = []
    offset = 84
    for _ in range(count):
        values = struct.unpack_from("<12fH", data, offset)
        tri: Triangle = (
            (float(values[3]), float(values[4]), float(values[5])),
            (float(values[6]), float(values[7]), float(values[8])),
            (float(values[9]), float(values[10]), float(values[11])),
        )
        if _valid_triangle(tri):
            triangles.append(tri)
        offset += 50
    return triangles


def _parse_ascii(data: bytes) -> list[Triangle]:
    try:
        text = data.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise STLReadError("STL is neither valid binary nor ASCII.") from exc
    vertices: list[Point3] = []
    triangles: list[Triangle] = []
    for line in text.splitlines():
        fields = line.strip().split()
        if len(fields) == 4 and fields[0].lower() == "vertex":
            try:
                vertices.append((float(fields[1]), float(fields[2]), float(fields[3])))
            except ValueError as exc:
                raise STLReadError("Invalid ASCII STL vertex.") from exc
            if len(vertices) == 3:
                tri: Triangle = (vertices[0], vertices[1], vertices[2])
                if _valid_triangle(tri):
                    triangles.append(tri)
                vertices.clear()
    return triangles


def _quantized(point: Point3, tolerance: float = 1e-6) -> tuple[int, int, int]:
    return tuple(round(v / tolerance) for v in point)  # type: ignore[return-value]


def is_watertight(triangles: list[Triangle]) -> bool:
    if not triangles:
        return False
    edges: Counter[tuple[tuple[int, int, int], tuple[int, int, int]]] = Counter()
    for tri in triangles:
        keys = [_quantized(p) for p in tri]
        for a, b in ((0, 1), (1, 2), (2, 0)):
            edge = tuple(sorted((keys[a], keys[b])))
            edges[edge] += 1
    return bool(edges) and all(count == 2 for count in edges.values())


def read_stl(path: str | Path) -> Mesh:
    source = Path(path)
    data = source.read_bytes()
    triangles: list[Triangle]
    binary_candidate = False
    if len(data) >= 84:
        count = struct.unpack_from("<I", data, 80)[0]
        binary_candidate = 84 + count * 50 == len(data)
    triangles = _parse_binary(data) if binary_candidate else _parse_ascii(data)
    if not triangles:
        raise STLReadError("No usable triangles were found in the STL.")
    return Mesh(
        triangles=triangles,
        source=source,
        fingerprint=hashlib.sha256(data).hexdigest(),
        watertight=is_watertight(triangles),
    )


def mesh_tiers(mesh: Mesh, analysis_limit: int = 80_000, preview_limit: int = 3_500) -> tuple[Mesh, Mesh]:
    """Return light analysis and topology-preserving preview tiers; original stays untouched."""
    def sample(limit: int) -> Mesh:
        if mesh.faces <= limit:
            tris = list(mesh.triangles)
        else:
            step = mesh.faces / limit
            tris = [mesh.triangles[min(int(i * step), mesh.faces - 1)] for i in range(limit)]
        return Mesh(tris, mesh.source, mesh.fingerprint, mesh.watertight)
    def clustered_preview(limit: int) -> Mesh:
        if mesh.faces <= limit:
            return Mesh(list(mesh.triangles), mesh.source, mesh.fingerprint, mesh.watertight)
        bounds = mesh.bounds
        size = bounds.size
        resolution = max(10, int(math.sqrt(limit / 7.0)))
        best: list[Triangle] = []
        while resolution >= 8:
            unique: dict[tuple[tuple[int, int, int], ...], Triangle] = {}

            def cell(point: Point3) -> tuple[int, int, int]:
                return tuple(
                    min(resolution, max(0, round((point[i] - bounds.minimum[i]) / max(size[i], 1e-12) * resolution)))
                    for i in range(3)
                )  # type: ignore[return-value]

            def centre(key: tuple[int, int, int]) -> Point3:
                return tuple(
                    bounds.minimum[i] + key[i] / resolution * size[i]
                    for i in range(3)
                )  # type: ignore[return-value]

            for triangle in mesh.triangles:
                keys = tuple(cell(point) for point in triangle)
                if len(set(keys)) < 3:
                    continue
                face_key = tuple(sorted(keys))
                if face_key not in unique:
                    candidate: Triangle = tuple(centre(key) for key in keys)  # type: ignore[assignment]
                    if _valid_triangle(candidate):
                        unique[face_key] = candidate
            best = list(unique.values())
            if len(best) <= limit:
                break
            resolution = int(resolution * 0.82)
        return Mesh(best, mesh.source, mesh.fingerprint, mesh.watertight)

    return sample(analysis_limit), clustered_preview(preview_limit)
