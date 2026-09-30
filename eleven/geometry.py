
from __future__ import annotations

import math
import threading
from collections import defaultdict
from typing import Callable, Iterable

import numpy as np

try:
    import trimesh
except Exception:  # pragma: no cover
    trimesh = None

from .models import Mesh, Point2, Point3, SliceJob, SlicePart, SliceSettings, Triangle


class TaskCancelled(RuntimeError):
    pass


def transform_mesh(mesh: Mesh, settings: SliceSettings) -> list[Triangle]:
    """Map STL X/Y/Z to final Width/Depth/Height without mutating source data."""
    settings.validate()
    bounds = mesh.bounds
    source_size = bounds.size
    if min(source_size) <= 1e-12:
        raise ValueError("STL has a zero-size axis and cannot be scaled safely.")
    sx = settings.dimensions.width / source_size[0]
    sy = settings.dimensions.depth / source_size[1]
    sz = settings.dimensions.height / source_size[2]
    ox, oy, oz = bounds.minimum
    half_w = settings.dimensions.width / 2.0
    half_h = settings.dimensions.height / 2.0
    depth = settings.dimensions.depth
    depth_dir = settings.depth_direction
    out: list[Triangle] = []
    append = out.append
    for tri in mesh.triangles:
        pts = []
        for p in tri:
            x = (p[0] - ox) * sx - half_w
            nd = (p[1] - oy) * sy
            if depth_dir == "backward":
                y = nd - depth
            elif depth_dir == "both":
                y = nd - depth * 0.5
            else:
                y = nd
            z = (p[2] - oz) * sz - half_h
            pts.append((x, y, z))
        append((pts[0], pts[1], pts[2]))
    return out


def _distance(a: Point2, b: Point2) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _triangle_plane_segment(triangle: Triangle, plane_x: float, epsilon: float) -> tuple[Point2, Point2] | None:
    points: list[Point2] = []
    for a, b in ((triangle[0], triangle[1]), (triangle[1], triangle[2]), (triangle[2], triangle[0])):
        da, db = a[0] - plane_x, b[0] - plane_x
        if abs(da) <= epsilon and abs(db) <= epsilon:
            continue
        if (da < -epsilon and db < -epsilon) or (da > epsilon and db > epsilon):
            continue
        denominator = da - db
        if abs(denominator) <= epsilon:
            continue
        t = da / denominator
        if -epsilon <= t <= 1.0 + epsilon:
            point = (a[1] + t * (b[1] - a[1]), a[2] + t * (b[2] - a[2]))
            if not any(_distance(point, existing) <= epsilon for existing in points):
                points.append(point)
    if len(points) == 2 and _distance(points[0], points[1]) > epsilon:
        return points[0], points[1]
    return None


def _snap_key(point: Point2, tolerance: float) -> tuple[int, int]:
    return round(point[0] / tolerance), round(point[1] / tolerance)


def _polygon_area(points: list[Point2]) -> float:
    return 0.5 * sum(
        points[i][0] * points[(i + 1) % len(points)][1]
        - points[(i + 1) % len(points)][0] * points[i][1]
        for i in range(len(points))
    )


def _deduplicate_segments(segments: Iterable[tuple[Point2, Point2]], tolerance: float) -> list[tuple[Point2, Point2]]:
    unique: dict[tuple[tuple[int, int], tuple[int, int]], tuple[Point2, Point2]] = {}
    for a, b in segments:
        ka, kb = _snap_key(a, tolerance), _snap_key(b, tolerance)
        if ka == kb:
            continue
        key = tuple(sorted((ka, kb)))  # type: ignore[assignment]
        unique[key] = (a, b)
    return list(unique.values())


def _stitch_segments_full(
    segments: list[tuple[Point2, Point2]], tolerance: float
) -> tuple[list[list[Point2]], list[list[Point2]]]:
    """Stitch unordered section segments into closed loops and remaining open chains."""
    segments = _deduplicate_segments(segments, tolerance)
    endpoints: dict[tuple[int, int], list[int]] = defaultdict(list)
    for i, (a, b) in enumerate(segments):
        endpoints[_snap_key(a, tolerance)].append(i)
        endpoints[_snap_key(b, tolerance)].append(i)
    unused = set(range(len(segments)))
    loops: list[list[Point2]] = []
    open_chains: list[list[Point2]] = []
    while unused:
        current_index = unused.pop()
        a, b = segments[current_index]
        chain = [a, b]
        start_key = _snap_key(a, tolerance)
        current_key = _snap_key(b, tolerance)
        while current_key != start_key:
            candidates = [i for i in endpoints[current_key] if i in unused]
            if not candidates:
                break
            next_index = candidates[0]
            unused.remove(next_index)
            c, d = segments[next_index]
            if _snap_key(c, tolerance) == current_key:
                next_point = d
            else:
                next_point = c
            chain.append(next_point)
            current_key = _snap_key(next_point, tolerance)
        if current_key == start_key and len(chain) >= 4:
            chain[-1] = chain[0]
            cleaned = chain[:-1]
            if abs(_polygon_area(cleaned)) > tolerance * tolerance:
                if _polygon_area(cleaned) < 0:
                    cleaned.reverse()
                loops.append(cleaned)
        else:
            if len(chain) >= 2:
                open_chains.append(chain)
    loops.sort(key=lambda loop: abs(_polygon_area(loop)), reverse=True)
    return loops, open_chains


def stitch_segments(segments: list[tuple[Point2, Point2]], tolerance: float) -> tuple[list[list[Point2]], int]:
    """Compatibility wrapper returning closed loops and the number of open chains."""
    loops, open_chains = _stitch_segments_full(segments, tolerance)
    return loops, len(open_chains)


def _cluster_segment_endpoints(
    segments: list[tuple[Point2, Point2]], tolerance: float
) -> tuple[np.ndarray, np.ndarray]:
    """Endpoint clustering copied from the proven v2.6.2 fallback slicer."""
    array = np.asarray(segments, dtype=float)
    if array.size == 0 or array.ndim != 3 or array.shape[1:] != (2, 2):
        return np.empty((0, 2)), np.empty((0, 2), dtype=int)
    array = array[np.all(np.isfinite(array), axis=(1, 2))]
    if len(array) == 0:
        return np.empty((0, 2)), np.empty((0, 2), dtype=int)
    tolerance = max(float(tolerance), 1e-7)
    buckets: dict[tuple[int, int], list[int]] = {}
    vertices: list[np.ndarray] = []
    counts: list[int] = []
    point_ids: list[int] = []
    for point in array.reshape(-1, 2):
        cell = tuple(np.floor(point / tolerance).astype(np.int64))
        best_id = None
        best_distance = None
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for vertex_id in buckets.get((cell[0] + dx, cell[1] + dy), []):
                    distance = float(np.linalg.norm(vertices[vertex_id] - point))
                    if distance <= tolerance and (best_distance is None or distance < best_distance):
                        best_id, best_distance = vertex_id, distance
        if best_id is None:
            best_id = len(vertices)
            vertices.append(point.copy())
            counts.append(1)
            buckets.setdefault(cell, []).append(best_id)
        else:
            count = counts[best_id]
            vertices[best_id] = (vertices[best_id] * count + point) / (count + 1)
            counts[best_id] += 1
        point_ids.append(best_id)
    edge_set: set[tuple[int, int]] = set()
    edges = []
    for a, b in np.asarray(point_ids, dtype=int).reshape(-1, 2):
        if a == b:
            continue
        key = (min(int(a), int(b)), max(int(a), int(b)))
        if key not in edge_set:
            edge_set.add(key)
            edges.append(key)
    return np.asarray(vertices, dtype=float), np.asarray(edges, dtype=int)


def _trace_graph_chains(vertices: np.ndarray, edges: np.ndarray) -> list[np.ndarray]:
    """Trace chains with the same straightest-branch rule used by v2.6.2."""
    if len(vertices) == 0 or len(edges) == 0:
        return []
    adjacency = {index: [] for index in range(len(vertices))}
    for edge_id, (a, b) in enumerate(edges):
        adjacency[int(a)].append((edge_id, int(b)))
        adjacency[int(b)].append((edge_id, int(a)))
    used: set[int] = set()
    chains: list[list[int]] = []

    def walk(start_vertex: int, start_edge: int) -> list[int]:
        chain, current, edge_id, previous = [start_vertex], start_vertex, start_edge, None
        while edge_id is not None and edge_id not in used:
            used.add(edge_id)
            a, b = edges[edge_id]
            next_vertex = int(b if int(a) == current else a)
            chain.append(next_vertex)
            previous, current = current, next_vertex
            if current == start_vertex:
                break
            candidates = [(candidate, other) for candidate, other in adjacency[current] if candidate not in used]
            if not candidates:
                break
            if previous is not None and len(candidates) > 1:
                incoming = vertices[current] - vertices[previous]
                incoming_norm = np.linalg.norm(incoming)
                if incoming_norm > 0:
                    incoming = incoming / incoming_norm
                    def score(item):
                        outgoing = vertices[item[1]] - vertices[current]
                        norm = np.linalg.norm(outgoing)
                        return float(np.dot(incoming, outgoing / norm)) if norm else -999.0
                    candidates.sort(key=score, reverse=True)
            edge_id = candidates[0][0]
        return chain

    for vertex, neighbors in adjacency.items():
        if len(neighbors) == 2:
            continue
        for edge_id, _ in neighbors:
            if edge_id not in used:
                chain = walk(vertex, edge_id)
                if len(chain) >= 2:
                    chains.append(chain)
    for edge_id, (a, _) in enumerate(edges):
        if edge_id not in used:
            chain = walk(int(a), edge_id)
            if len(chain) >= 2:
                chains.append(chain)
    return [vertices[np.asarray(chain, dtype=int)] for chain in chains]


def v262_repair_loops(
    segments: list[tuple[Point2, Point2]], initial_tolerance: float, back_y: float | None = None
) -> tuple[list[list[Point2]], int, float]:
    """Rebuild open relief sections exactly like the v2.6.2 graph fallback."""
    tolerances = sorted(set([
        round(max(0.001, initial_tolerance), 4),
        0.02, 0.05, 0.10, 0.20, 0.25, 0.35, 0.50, 0.75, 1.00,
    ]))
    best: tuple[tuple[int, int, float], list[list[Point2]], int, float] | None = None
    for tolerance in tolerances:
        vertices, edges = _cluster_segment_endpoints(segments, tolerance)
        chains = _trace_graph_chains(vertices, edges)
        if not chains:
            continue
        lengths = [float(np.linalg.norm(np.diff(chain, axis=0), axis=1).sum()) if len(chain) >= 2 else 0.0 for chain in chains]
        primary_open = int(np.argmax(lengths)) if lengths else None
        loops: list[list[Point2]] = []
        open_count = 0
        close_tolerance = max(tolerance * 2.0, 1e-5)
        for chain_index, points in enumerate(chains):
            if len(points) < 2:
                continue
            keep = [0]
            for index in range(1, len(points)):
                if np.linalg.norm(points[index] - points[keep[-1]]) > 1e-8:
                    keep.append(index)
            points = points[keep]
            if len(points) < 3:
                continue
            gap = float(np.linalg.norm(points[0] - points[-1]))
            is_closed = gap <= close_tolerance
            is_primary_relief = chain_index == primary_open
            if not is_closed and not is_primary_relief:
                open_count += 1
                continue
            if gap > 1e-8:
                if is_primary_relief and back_y is not None:
                    # Open relief skins are closed to the real flat wall plane,
                    # not by a diagonal guess between their two loose endpoints.
                    points = np.vstack([
                        points,
                        np.asarray((back_y, points[-1][1]), dtype=float),
                        np.asarray((back_y, points[0][1]), dtype=float),
                        points[0],
                    ])
                else:
                    points = np.vstack([points, points[0]])
            loop = [(float(point[0]), float(point[1])) for point in points[:-1]]
            if len(loop) >= 3 and abs(_polygon_area(loop)) > tolerance * tolerance:
                if _polygon_area(loop) < 0:
                    loop.reverse()
                loops.append(loop)
        if loops:
            score = (1, -open_count, -tolerance)
            candidate = (score, loops, open_count, tolerance)
            if best is None or candidate[0] > best[0]:
                best = candidate
            if open_count == 0:
                break
    return (best[1], best[2], best[3]) if best else ([], 0, initial_tolerance)


def _point_line_distance(point: Point2, start: Point2, end: Point2) -> float:
    dx, dy = end[0] - start[0], end[1] - start[1]
    if dx == 0 and dy == 0:
        return _distance(point, start)
    t = max(0.0, min(1.0, ((point[0] - start[0]) * dx + (point[1] - start[1]) * dy) / (dx * dx + dy * dy)))
    projection = (start[0] + t * dx, start[1] + t * dy)
    return _distance(point, projection)


def simplify_closed(points: list[Point2], tolerance: float) -> list[Point2]:
    if tolerance <= 0 or len(points) <= 4:
        return points
    closed = points + [points[0]]

    def rdp(line: list[Point2]) -> list[Point2]:
        if len(line) <= 2:
            return line
        distances = [_point_line_distance(line[i], line[0], line[-1]) for i in range(1, len(line) - 1)]
        maximum = max(distances, default=0.0)
        if maximum <= tolerance:
            return [line[0], line[-1]]
        split = distances.index(maximum) + 1
        return rdp(line[: split + 1])[:-1] + rdp(line[split:])

    # Split the ring at the point farthest from point zero to avoid collapsing a closed ring.
    pivot = max(range(1, len(points)), key=lambda i: _distance(points[0], points[i]))
    first = rdp(points[: pivot + 1])
    second = rdp(points[pivot:] + [points[0]])
    result = first[:-1] + second[:-1]
    return result if len(result) >= 3 else points


def point_in_material(point: Point2, loops: list[list[Point2]]) -> bool:
    """Even/odd fill supports outer contours, islands and void loops."""
    inside = False
    x, y = point
    for polygon in loops:
        local = False
        j = len(polygon) - 1
        for i, pi in enumerate(polygon):
            pj = polygon[j]
            if ((pi[1] > y) != (pj[1] > y)) and x < (pj[0] - pi[0]) * (y - pi[1]) / (pj[1] - pi[1]) + pi[0]:
                local = not local
            j = i
        if local:
            inside = not inside
    return inside


def edge_clearance(point: Point2, loops: list[list[Point2]]) -> float:
    if not loops:
        return 0.0
    return min(
        _point_line_distance(point, polygon[i], polygon[(i + 1) % len(polygon)])
        for polygon in loops
        for i in range(len(polygon))
    )


def best_inside_point(
    loops: list[list[Point2]],
    minimum_clearance: float = 0.0,
    avoid: Iterable[Point2] = (),
    avoid_distance: float = 0.0,
) -> Point2 | None:
    if not loops:
        return None
    xs = [p[0] for loop in loops for p in loop]
    ys = [p[1] for loop in loops for p in loop]
    candidates: list[Point2] = []
    for ix in range(1, 12):
        for iy in range(1, 12):
            candidates.append((min(xs) + (max(xs) - min(xs)) * ix / 12, min(ys) + (max(ys) - min(ys)) * iy / 12))
    avoided = list(avoid)
    valid = [
        p for p in candidates
        if point_in_material(p, loops)
        and all(_distance(p, other) >= avoid_distance for other in avoided)
    ]
    if not valid:
        return None
    best = max(valid, key=lambda p: edge_clearance(p, loops))
    return best if edge_clearance(best, loops) >= minimum_clearance else None


def safe_number_placement(loops: list[list[Point2]], requested_height: float) -> tuple[Point2, float] | None:
    """Compact v2.6-style number placement with automatic text-size reduction."""
    if not loops:
        return None
    xs = [point[0] for loop in loops for point in loop]
    ys = [point[1] for loop in loops for point in loop]
    candidates = []
    for ix in range(1, 18):
        for iy in range(1, 18):
            point = (min(xs) + (max(xs) - min(xs)) * ix / 18, min(ys) + (max(ys) - min(ys)) * iy / 18)
            if point_in_material(point, loops):
                candidates.append(point)
    requested_height = max(1.0, requested_height)
    for factor in (1.0, 0.9, 0.8, 0.7, 0.6, 0.5):
        height = max(1.0, requested_height * factor)
        half_width, half_height = 2.35 * height / 2.0, 1.25 * height / 2.0
        fitting = []
        for x, y in candidates:
            corners = [(x - half_width, y - half_height), (x + half_width, y - half_height), (x + half_width, y + half_height), (x - half_width, y + half_height)]
            if all(point_in_material(corner, loops) for corner in corners):
                fitting.append((x, y))
        if fitting:
            return max(fitting, key=lambda point: edge_clearance(point, loops)), height
    fallback = best_inside_point(loops, minimum_clearance=0.0)
    return (fallback, 1.1) if fallback else None



def _front_depth_at_z(polygon: list[Point2], z: float, back_y: float, front_sign: float) -> float | None:
    """Front-most depth of a contour along a horizontal line at height z."""
    best: float | None = None
    count = len(polygon)
    for i in range(count):
        a, b = polygon[i], polygon[(i + 1) % count]
        za, zb = a[1], b[1]
        if abs(zb - za) < 1e-12:
            continue
        if (z < min(za, zb) - 1e-9) or (z > max(za, zb) + 1e-9):
            continue
        t = (z - za) / (zb - za)
        if t < -1e-6 or t > 1.0 + 1e-6:
            continue
        y = a[0] + t * (b[0] - a[0])
        signed = (y - back_y) * front_sign
        if best is None or signed > best:
            best = signed
    return best


def unify_islands_on_back(
    loops: list[list[Point2]],
    back_y: float,
    outward_sign: float,
    min_area: float = 8.0,
) -> list[list[Point2]]:
    """Join every island on a slice into one cuttable piece via the rear rail."""
    if not loops:
        return []
    front_sign = -outward_sign
    outers: list[list[Point2]] = []
    for loop in loops:
        if len(loop) < 3:
            continue
        if abs(_polygon_area(loop)) < min_area:
            continue
        outers.append(loop)
    if not outers:
        return []
    if len(outers) == 1:
        return outers

    zmin = min(p[1] for loop in outers for p in loop)
    zmax = max(p[1] for loop in outers for p in loop)
    if zmax - zmin < 0.5:
        return [max(outers, key=lambda loop: abs(_polygon_area(loop)))]

    samples = max(80, min(400, int((zmax - zmin) * 4)))
    front: list[Point2] = []
    for i in range(samples + 1):
        z = zmin + (zmax - zmin) * i / samples
        best: float | None = None
        for loop in outers:
            signed = _front_depth_at_z(loop, z, back_y, front_sign)
            if signed is None:
                continue
            if best is None or signed > best:
                best = signed
        if best is None:
            continue
        y = back_y + front_sign * max(best, 0.2)
        if not front or abs(front[-1][0] - y) > 1e-6 or abs(front[-1][1] - z) > 1e-6:
            front.append((y, z))
    if len(front) < 2:
        return [max(outers, key=lambda loop: abs(_polygon_area(loop)))]

    closed = list(front) + [(back_y, front[-1][1]), (back_y, front[0][1])]
    cleaned: list[Point2] = []
    for point in closed:
        if not cleaned or abs(point[0] - cleaned[-1][0]) > 1e-9 or abs(point[1] - cleaned[-1][1]) > 1e-9:
            cleaned.append(point)
    if len(cleaned) >= 3 and _polygon_area(cleaned) < 0:
        cleaned.reverse()
    return [cleaned] if len(cleaned) >= 3 else [max(outers, key=lambda loop: abs(_polygon_area(loop)))]


def slice_count_from_quality(width: float, thickness: float, gap: float, quality: str) -> int:
    """Slicer-for-Fusion style: high ≈ one sheet per material thickness across the model width."""
    step = max(thickness + gap, 0.5)
    packed = max(2, int(round(width / step)))
    # very_low..high map to coarser → denser stacks (like reducing slice count in Fusion Slicer)
    factor = {
        "very_low": 0.30,
        "low": 0.50,
        "medium": 0.72,
        "high": 1.00,
    }.get(quality, 0.72)
    return max(6, min(packed, int(round(packed * factor))))



def _loop_area(loops: list[list[Point2]]) -> float:
    return sum(abs(_polygon_area(loop)) for loop in loops)


def _loop_bbox(loops: list[list[Point2]]) -> tuple[float, float, float, float]:
    pts = [p for loop in loops for p in loop]
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return min(xs), min(ys), max(xs), max(ys)


def _sample_loop(loop: list[Point2], n: int = 48) -> list[Point2]:
    if len(loop) < 2:
        return list(loop)
    # cumulative length sampling
    segs = []
    total = 0.0
    for i in range(len(loop)):
        a, b = loop[i], loop[(i + 1) % len(loop)]
        d = _distance(a, b)
        segs.append((a, b, d))
        total += d
    if total < 1e-9:
        return [loop[0]] * n
    out = []
    for k in range(n):
        target = (k / n) * total
        acc = 0.0
        for a, b, d in segs:
            if acc + d >= target:
                t = 0.0 if d < 1e-12 else (target - acc) / d
                out.append((a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1])))
                break
            acc += d
        else:
            out.append(loop[-1])
    return out


def parts_contour_distance(a: list[list[Point2]], b: list[list[Point2]]) -> float:
    """Approximate Hausdorff between primary outer loops (mm)."""
    if not a or not b:
        return 1e9
    la = max(a, key=lambda loop: abs(_polygon_area(loop)))
    lb = max(b, key=lambda loop: abs(_polygon_area(loop)))
    sa, sb = _sample_loop(la), _sample_loop(lb)
    def directed(src, dst):
        return max(min(_distance(p, q) for q in dst) for p in src)
    return max(directed(sa, sb), directed(sb, sa))


def merge_max_curve_loops(
    loops_a: list[list[Point2]],
    loops_b: list[list[Point2]],
    back_y: float,
    outward_sign: float,
) -> list[list[Point2]]:
    """Keep the outer envelope of two similar slices so one part holds the fuller curve."""
    combined = list(loops_a) + list(loops_b)
    return unify_islands_on_back(
        combined, back_y=back_y, outward_sign=outward_sign, min_area=max(12.0, 8.0)
    )


def collapse_near_duplicate_parts(
    parts: list[SlicePart],
    settings: SliceSettings,
    similarity_mm: float | None = None,
) -> tuple[list[SlicePart], int]:
    """Drop / merge consecutive slices whose outline barely changes.

    Near-identical layers (e.g. part 5 vs 6) become one row that keeps the
    maximum silhouette curve so the stacked 3D look stays intact.
    """
    if len(parts) < 2:
        return parts, 0
    diag = math.hypot(settings.dimensions.depth, settings.dimensions.height)
    threshold = similarity_mm if similarity_mm is not None else max(0.9, min(3.0, diag * 0.012))
    back_y = float(getattr(settings, "back_plane_offset", 0.0) or 0.0)
    outward = 1.0 if settings.depth_direction == "backward" else -1.0
    min_area = max(20.0, settings.minimum_web * settings.minimum_web * 0.5)

    filtered: list[SlicePart] = []
    for part in parts:
        area = _loop_area(part.loops)
        if area < min_area:
            continue
        xmin, ymin, xmax, ymax = _loop_bbox(part.loops)
        if (ymax - ymin) < max(3.0, settings.female_slot_length * 0.25):
            continue
        filtered.append(part)

    if not filtered:
        return parts, 0

    result: list[SlicePart] = [filtered[0]]
    merged = 0
    for nxt in filtered[1:]:
        cur = result[-1]
        dist = parts_contour_distance(cur.loops, nxt.loops)
        area_a, area_b = _loop_area(cur.loops), _loop_area(nxt.loops)
        area_ratio = abs(area_a - area_b) / max(area_a, area_b, 1e-6)
        if dist <= threshold and area_ratio <= 0.08:
            # Merge into one part with the fuller outer curve.
            merged_loops = merge_max_curve_loops(cur.loops, nxt.loops, back_y, outward)
            if not merged_loops:
                merged_loops = cur.loops if area_a >= area_b else nxt.loops
            number = safe_number_placement(merged_loops, min(settings.dimensions.height * 0.012 * getattr(settings, 'number_scale', 0.55) / 0.55, 4.0))
            result[-1] = SlicePart(
                index=cur.index,
                x=0.5 * (cur.x + nxt.x),
                loops=merged_loops,
                number_position=number[0] if number else cur.number_position,
                number_height=number[1] if number else cur.number_height,
            )
            merged += 1
        else:
            result.append(nxt)
    for i, part in enumerate(result, start=1):
        part.index = i
    return result, merged



def _trimesh_from_triangles(triangles: list[Triangle], max_faces: int = 180_000):
    """Build a welded trimesh for plane sections (interactive-stl-slicer style / MIT)."""
    if trimesh is None or not triangles:
        return None
    n = len(triangles)
    # Optional stride on ultra-dense meshes keeps section interactive.
    step = max(1, int(math.ceil(n / max_faces))) if n > max_faces else 1
    verts: list[tuple[float, float, float]] = []
    faces: list[list[int]] = []
    index: dict[tuple[int, int, int], int] = {}
    quant = 1e6  # µm grid for welding

    def vid(p: Point3) -> int:
        key = (int(round(p[0] * quant)), int(round(p[1] * quant)), int(round(p[2] * quant)))
        existing = index.get(key)
        if existing is not None:
            return existing
        i = len(verts)
        verts.append((float(p[0]), float(p[1]), float(p[2])))
        index[key] = i
        return i

    for i in range(0, n, step):
        a, b, c = triangles[i]
        faces.append([vid(a), vid(b), vid(c)])
    if not faces:
        return None
    mesh = trimesh.Trimesh(
        vertices=np.asarray(verts, dtype=np.float64),
        faces=np.asarray(faces, dtype=np.int64),
        process=False,
    )
    # Cache centroid once for section origins
    try:
        _ = mesh.centroid
    except Exception:
        pass
    return mesh


def _loops_from_trimesh_x_section(tm, plane_x: float, cy: float, cz: float) -> list[list[Point2]]:
    """Slice at X=plane_x using trimesh.section; project to (Y,Z) plane."""
    if tm is None:
        return []
    try:
        path3 = tm.section(plane_origin=[float(plane_x), cy, cz], plane_normal=[1.0, 0.0, 0.0])
    except Exception:
        return []
    if path3 is None:
        return []
    loops: list[list[Point2]] = []
    discrete = getattr(path3, "discrete", None) or []
    for entity in discrete:
        if entity is None or len(entity) < 3:
            continue
        pts: list[Point2] = [(float(p[1]), float(p[2])) for p in entity]
        if len(pts) >= 2 and _distance(pts[0], pts[-1]) > 1e-6:
            pts.append(pts[0])
        if len(pts) >= 2 and _distance(pts[0], pts[-1]) <= 1e-6:
            pts = pts[:-1]
        if len(pts) >= 3:
            loops.append(pts)
    return loops




def crop_aabb_from_settings(settings: SliceSettings) -> tuple[float, float, float, float, float, float] | None:
    """Return (xmin,xmax, ymin,ymax, zmin,zmax) in transformed coordinates, or None if crop off."""
    if not getattr(settings, "crop_enabled", False):
        return None
    half_w = settings.dimensions.width / 2.0
    half_h = settings.dimensions.height / 2.0
    depth = settings.dimensions.depth
    # Transformed space: X centred [-half_w, half_w], Z centred [-half_h, half_h],
    # Y: forward → [0, depth], backward → [-depth, 0]
    xmin = -half_w + settings.crop_width_min * settings.dimensions.width
    xmax = -half_w + settings.crop_width_max * settings.dimensions.width
    zmin = -half_h + settings.crop_height_min * settings.dimensions.height
    zmax = -half_h + settings.crop_height_max * settings.dimensions.height
    if settings.depth_direction == "backward":
        ymin = -depth + settings.crop_depth_min * depth
        ymax = -depth + settings.crop_depth_max * depth
    elif settings.depth_direction == "both":
        ymin = -depth * 0.5 + settings.crop_depth_min * depth
        ymax = -depth * 0.5 + settings.crop_depth_max * depth
    else:
        ymin = settings.crop_depth_min * depth
        ymax = settings.crop_depth_max * depth
    return xmin, xmax, ymin, ymax, zmin, zmax


def apply_crop_to_triangles(triangles: list[Triangle], settings: SliceSettings) -> list[Triangle]:
    """Keep triangles whose centroid lies inside the crop AABB (Corel-style region crop)."""
    box = crop_aabb_from_settings(settings)
    if box is None:
        return triangles
    xmin, xmax, ymin, ymax, zmin, zmax = box
    kept: list[Triangle] = []
    for tri in triangles:
        cx = (tri[0][0] + tri[1][0] + tri[2][0]) / 3.0
        cy = (tri[0][1] + tri[1][1] + tri[2][1]) / 3.0
        cz = (tri[0][2] + tri[1][2] + tri[2][2]) / 3.0
        if xmin <= cx <= xmax and ymin <= cy <= ymax and zmin <= cz <= zmax:
            kept.append(tri)
    return kept


def offset_polygon_outward(points: list[Point2], distance: float) -> list[Point2]:
    """Simple outward offset for a closed CCW polygon (positive area). Used for bold outlines."""
    if distance <= 1e-9 or len(points) < 3:
        return points
    n = len(points)
    # Ensure CCW
    work = list(points)
    if _polygon_area(work) < 0:
        work = list(reversed(work))
    result: list[Point2] = []
    for i in range(n):
        prev = work[(i - 1) % n]
        curr = work[i]
        nxt = work[(i + 1) % n]
        e1 = (curr[0] - prev[0], curr[1] - prev[1])
        e2 = (nxt[0] - curr[0], nxt[1] - curr[1])
        len1 = math.hypot(e1[0], e1[1]) or 1e-12
        len2 = math.hypot(e2[0], e2[1]) or 1e-12
        # outward normals (right-hand for CCW = rotate edge 90 deg CW? for CCW outward is rotate edge 90 deg clockwise = (y, -x) wait)
        # CCW polygon: left normal is inward; right normal is outward.
        # Edge (dx,dy) → right normal (dy, -dx)
        n1 = (e1[1] / len1, -e1[0] / len1)
        n2 = (e2[1] / len2, -e2[0] / len2)
        bis = (n1[0] + n2[0], n1[1] + n2[1])
        bl = math.hypot(bis[0], bis[1])
        if bl < 1e-9:
            # nearly collinear — push along n1
            result.append((curr[0] + n1[0] * distance, curr[1] + n1[1] * distance))
            continue
        bis = (bis[0] / bl, bis[1] / bl)
        # scale so the offset distance along the bisector matches `distance`
        # cos(half) approx = dot(n1, bis)
        cos_h = max(0.15, n1[0] * bis[0] + n1[1] * bis[1])
        scale = distance / cos_h
        scale = min(scale, distance * 4.0)  # clamp spikes
        result.append((curr[0] + bis[0] * scale, curr[1] + bis[1] * scale))
    if len(result) >= 3 and _polygon_area(result) < 0:
        result.reverse()
    return result


def apply_bold_to_parts(parts: list[SlicePart], settings: SliceSettings) -> None:
    """Expand the largest (outer) loop of each part outward by bold_offset mm."""
    if not getattr(settings, "bold_enabled", False):
        return
    dist = float(getattr(settings, "bold_offset", 0.0) or 0.0)
    if dist <= 0:
        return
    for part in parts:
        if not part.loops:
            continue
        outer_i = max(range(len(part.loops)), key=lambda i: abs(_polygon_area(part.loops[i])))
        part.loops[outer_i] = offset_polygon_outward(part.loops[outer_i], dist)


def slice_mesh(
    mesh: Mesh,
    settings: SliceSettings,
    cancel_event: threading.Event | None = None,
    progress: Callable[[float], None] | None = None,
) -> SliceJob:
    settings.validate()
    transformed = transform_mesh(mesh, settings)
    if getattr(settings, "crop_enabled", False):
        before = len(transformed)
        transformed = apply_crop_to_triangles(transformed, settings)
        if not transformed:
            raise ValueError("Crop region contains no mesh faces; widen the crop box.")
    half_width = settings.dimensions.width / 2.0
    # Slice at equal cell centres, never on the model's tangent boundary. This prevents
    # zero-area/tiny first and last parts while preserving the requested overall scale.
    planes = [
        -half_width + (i + 0.5) * settings.dimensions.width / settings.count
        for i in range(settings.count)
    ]
    section_tolerance = max(settings.dimensions.height, settings.dimensions.depth, 1.0) * 1e-6
    plane_spacing = settings.dimensions.width / settings.count
    # Prefer trimesh.section (interactive-stl-slicer style) for cleaner closed curves.
    tm_work = _trimesh_from_triangles(transformed)
    use_trimesh_section = tm_work is not None
    cy = cz = 0.0
    if use_trimesh_section:
        try:
            c = tm_work.centroid
            cy, cz = float(c[1]), float(c[2])
        except Exception:
            cy = cz = 0.0

    # Only build triangle buckets if we may fall back to the segment stitcher.
    buckets: list[list[Triangle]] | None = None
    if not use_trimesh_section:
        buckets = [[] for _ in planes]
        for triangle_index, tri in enumerate(transformed):
            if cancel_event and triangle_index % 4096 == 0 and cancel_event.is_set():
                raise TaskCancelled("Slice task cancelled.")
            min_x = min(p[0] for p in tri) - section_tolerance
            max_x = max(p[0] for p in tri) + section_tolerance
            first = max(0, math.ceil((min_x + half_width) / plane_spacing - 0.5))
            last = min(settings.count - 1, math.floor((max_x + half_width) / plane_spacing - 0.5))
            for bucket_index in range(first, last + 1):
                buckets[bucket_index].append(tri)

    parts: list[SlicePart] = []
    total_open_chains = 0
    repaired_relief_slices = 0
    repair_tolerances: list[float] = []
    fallback_buckets_built = False
    for index, plane in enumerate(planes, start=1):
        if cancel_event and cancel_event.is_set():
            raise TaskCancelled("Slice task cancelled.")
        loops: list[list[Point2]] = []
        open_chains: list = []
        if use_trimesh_section:
            loops = _loops_from_trimesh_x_section(tm_work, plane, cy, cz)
        if not loops:
            if buckets is None and not fallback_buckets_built:
                # Rare path: trimesh failed on a plane — build buckets once.
                buckets = [[] for _ in planes]
                for triangle_index, tri in enumerate(transformed):
                    if cancel_event and triangle_index % 8192 == 0 and cancel_event.is_set():
                        raise TaskCancelled("Slice task cancelled.")
                    min_x = min(p[0] for p in tri) - section_tolerance
                    max_x = max(p[0] for p in tri) + section_tolerance
                    first = max(0, math.ceil((min_x + half_width) / plane_spacing - 0.5))
                    last = min(settings.count - 1, math.floor((max_x + half_width) / plane_spacing - 0.5))
                    for bucket_index in range(first, last + 1):
                        buckets[bucket_index].append(tri)
                fallback_buckets_built = True
            segments = []
            if buckets is not None:
                for tri in buckets[index - 1]:
                    segment = _triangle_plane_segment(tri, plane, section_tolerance)
                    if segment:
                        segments.append(segment)
            loops, open_chains = _stitch_segments_full(segments, section_tolerance * 5)
            total_open_chains += len(open_chains)
            if not loops and open_chains:
                back_y = 0.0 if settings.depth_direction in ("forward", "backward") else None
                repaired, remaining_open, used_tolerance = v262_repair_loops(segments, 0.25, back_y=back_y)
                if repaired:
                    loops = repaired
                    repaired_relief_slices += 1
                    total_open_chains += remaining_open
                    repair_tolerances.append(used_tolerance)
        loops = [simplify_closed(loop, settings.simplify_tolerance) for loop in loops]
        back_y = float(getattr(settings, "back_plane_offset", 0.0) or 0.0)
        outward = 1.0 if settings.depth_direction == "backward" else -1.0
        loops = unify_islands_on_back(
            loops, back_y=back_y, outward_sign=outward, min_area=max(20.0, settings.minimum_web * 2.0)
        )
        number = safe_number_placement(loops, min(settings.dimensions.height * 0.012 * getattr(settings, 'number_scale', 0.55) / 0.55, 4.0))
        parts.append(SlicePart(
            index=index,
            x=plane,
            loops=loops,
            number_position=number[0] if number else None,
            number_height=number[1] if number else 1.2,
        ))
        if progress:
            progress(index / len(planes))
    requested_parts = len(parts)
    parts = [part for part in parts if part.loops]
    # Accept any run that yields at least two solid slices, or at least ~30% of the
    # requested planes. The stricter 50% gate rejected many open-relief STLs that
    # still produce a usable stacked assembly after empty edge planes are dropped.
    minimum_usable = max(2, math.ceil(requested_parts * 0.30))
    if len(parts) < minimum_usable:
        raise ValueError(
            f"Only {len(parts)} of {requested_parts} slices could be reconstructed; choose another slice direction."
        )
    skipped_parts = requested_parts - len(parts)
    # Merge near-identical consecutive layers; keep the max curve of the group.
    parts, merged_similar = collapse_near_duplicate_parts(parts, settings)
    for new_index, part in enumerate(parts, start=1):
        part.index = new_index
    apply_bold_to_parts(parts, settings)
    warnings = []
    if getattr(settings, "crop_enabled", False):
        warnings.append(
            f"Crop active: W {settings.crop_width_min*100:.0f}–{settings.crop_width_max*100:.0f}% · "
            f"H {settings.crop_height_min*100:.0f}–{settings.crop_height_max*100:.0f}% · "
            f"D {settings.crop_depth_min*100:.0f}–{settings.crop_depth_max*100:.0f}%."
        )
    if getattr(settings, "bold_enabled", False) and getattr(settings, "bold_offset", 0) > 0:
        warnings.append(f"Bold outline: outer contours expanded by {settings.bold_offset:.2f} mm.")
    if skipped_parts:
        warnings.append(
            f"v2.6.2 rule omitted {skipped_parts} empty edge/gap planes; {len(parts)} physical slices remain."
        )
    if merged_similar:
        warnings.append(
            f"{merged_similar} near-identical consecutive layers were merged into one row each "
            f"(kept the fuller silhouette curve)."
        )
    if repaired_relief_slices:
        warnings.append(
            f"v2.6.2 graph repair rebuilt {repaired_relief_slices} open relief slices "
            f"(max tolerance {max(repair_tolerances):.2f} mm)."
        )
    if total_open_chains and repaired_relief_slices < settings.count:
        warnings.append(f"{total_open_chains} open section chains were detected; inspect the non-watertight source.")
    if use_trimesh_section:
        warnings.append("Section engine: trimesh.section (interactive-stl-slicer style) with ELEVEN fallback.")
    else:
        warnings.append("Section engine: built-in segment stitcher (install trimesh for cleaner curves).")
    if not mesh.watertight:
        warnings.append("Source STL is non-watertight; every generated part must be checked before cutting.")
    return SliceJob(parts=parts, settings=settings, source_fingerprint=mesh.fingerprint, warnings=warnings)
