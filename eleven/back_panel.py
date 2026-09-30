
from __future__ import annotations

from .geometry import _polygon_area, safe_number_placement
from .models import BackPanel, BackSlot, Point2, SliceJob


def _walk(points: list[Point2], start: int, end: int, step: int) -> list[Point2]:
    result = [points[start]]
    index = start
    while index != end:
        index = (index + step) % len(points)
        result.append(points[index])
    return result


def _close_to_flat_back(
    polygon: list[Point2],
    back_y: float,
    outward_sign: float,
    min_section_depth: float,
    minimum_web: float,
) -> list[Point2]:
    """Force every outer contour onto a synthetic flat rear plane.

    Works for open reliefs and curved backs that never touch a common Y plane.
    The flat rear stays at back_y; front material is pushed to at least
    min_section_depth (and minimum_web) away from that plane.
    """
    if len(polygon) < 3:
        raise ValueError("A slice contour is too small for a back-panel tab.")

    front_sign = -outward_sign
    # Depth of each vertex measured from the synthetic rear toward the front.
    signed = [(point[0] - back_y) * front_sign for point in polygon]
    max_signed = max(signed) if signed else 0.0
    target_depth = max(min_section_depth, minimum_web, 0.0)

    # Keep the visible front silhouette; only raise points that sit behind the
    # required minimum section so the artistic profile is not flattened.
    reinforced: list[Point2] = []
    for (depth, vertical), s in zip(polygon, signed):
        if s < target_depth:
            depth = back_y + front_sign * target_depth
        reinforced.append((depth, vertical))

    # Sort by vertical axis to build a clean front path, then close on the rear.
    ordered = sorted(range(len(reinforced)), key=lambda i: reinforced[i][1])
    # Prefer the chain that stays farthest from the rear (the true front).
    # Walk the original ring starting from lowest Z so topology is preserved.
    low_i = min(range(len(reinforced)), key=lambda i: (reinforced[i][1], reinforced[i][0]))
    high_i = max(range(len(reinforced)), key=lambda i: (reinforced[i][1], reinforced[i][0]))

    forward = _walk(reinforced, low_i, high_i, 1)
    reverse = _walk(reinforced, low_i, high_i, -1)

    def excursion(path: list[Point2]) -> float:
        return max(((p[0] - back_y) * front_sign for p in path), default=0.0)

    front = forward if excursion(forward) >= excursion(reverse) else reverse
    if front[0][1] > front[-1][1]:
        front.reverse()

    # Deduplicate consecutive points.
    cleaned_front: list[Point2] = []
    for point in front:
        if (
            not cleaned_front
            or abs(point[0] - cleaned_front[-1][0]) > 1e-9
            or abs(point[1] - cleaned_front[-1][1]) > 1e-9
        ):
            cleaned_front.append(point)
    if len(cleaned_front) < 2:
        # Degenerate section: build a minimal rectangle of min_section_depth.
        z0 = min(p[1] for p in polygon)
        z1 = max(p[1] for p in polygon)
        if z1 - z0 < 1.0:
            z1 = z0 + 1.0
        front_y = back_y + front_sign * max(target_depth, 1.0)
        cleaned_front = [(front_y, z0), (front_y, z1)]

    low_z = cleaned_front[0][1]
    high_z = cleaned_front[-1][1]
    # Close contour: front path + rear edge (high → low).
    closed = list(cleaned_front) + [(back_y, high_z), (back_y, low_z)]
    cleaned: list[Point2] = []
    for point in closed:
        if not cleaned or abs(point[0] - cleaned[-1][0]) > 1e-9 or abs(point[1] - cleaned[-1][1]) > 1e-9:
            cleaned.append(point)
    if len(cleaned) >= 3 and _polygon_area(cleaned) < 0:
        cleaned.reverse()
    return cleaned


def _find_or_build_rear_edge(
    polygon: list[Point2],
    back_y: float,
    outward_sign: float,
    min_section_depth: float,
    minimum_web: float,
    force_flat_back: bool,
) -> tuple[list[Point2], float, float]:
    """Return (contour with flat rear, low_z, high_z) of the rear edge."""
    depth_span = max(point[0] for point in polygon) - min(point[0] for point in polygon)
    tolerance = max(0.05, depth_span * 0.01, 0.1)
    rear = [index for index, point in enumerate(polygon) if abs(point[0] - back_y) <= tolerance]

    if len(rear) >= 2 and not force_flat_back:
        low_index = min(rear, key=lambda index: polygon[index][1])
        high_index = max(rear, key=lambda index: polygon[index][1])
        return polygon, polygon[low_index][1], polygon[high_index][1]

    # Synthetic flat rear for curved / open backs.
    closed = _close_to_flat_back(polygon, back_y, outward_sign, min_section_depth, minimum_web)
    rear2 = [index for index, point in enumerate(closed) if abs(point[0] - back_y) <= max(tolerance, 0.15)]
    if len(rear2) < 2:
        # Absolute fallback: force the two lowest-depth vertices onto back_y.
        zs = [p[1] for p in closed]
        low_z, high_z = min(zs), max(zs)
        return closed, low_z, high_z
    low_index = min(rear2, key=lambda index: closed[index][1])
    high_index = max(rear2, key=lambda index: closed[index][1])
    return closed, closed[low_index][1], closed[high_index][1]


def _tab_centers_along_edge(low_z: float, high_z: float, tab_length: float, tab_count: int) -> list[float]:
    """Evenly space tab centres along the rear edge, all fitting inside the edge."""
    span = high_z - low_z
    if tab_count < 1:
        tab_count = 1
    needed = tab_count * tab_length + 0.2 * max(0, tab_count - 1)
    if span < needed:
        # Shrink effective tab length so every tab still fits; caller may warn.
        effective = max(1.0, (span - 0.2 * max(0, tab_count - 1)) / tab_count)
    else:
        effective = tab_length
    usable = span - effective
    if tab_count == 1 or usable <= 0:
        return [(low_z + high_z) / 2.0]
    step = usable / (tab_count - 1)
    return [low_z + effective / 2.0 + i * step for i in range(tab_count)]


def _add_tabs(
    polygon: list[Point2],
    back_y: float,
    tab_depth: float,
    tab_length: float,
    outward_sign: float,
    minimum_web: float,
    min_section_depth: float,
    tab_count: int,
    force_flat_back: bool,
) -> tuple[list[Point2], list[float]]:
    """Replace the rear edge with one or more concentric male tabs."""
    contour, low_z, high_z = _find_or_build_rear_edge(
        polygon, back_y, outward_sign, min_section_depth, minimum_web, force_flat_back
    )

    # Rebuild front path between rear endpoints after possible synthetic close.
    depth_span = max(p[0] for p in contour) - min(p[0] for p in contour)
    tolerance = max(0.05, depth_span * 0.01, 0.1)
    rear = [i for i, p in enumerate(contour) if abs(p[0] - back_y) <= tolerance]
    if len(rear) < 2:
        rear = sorted(range(len(contour)), key=lambda i: abs(contour[i][0] - back_y))[:2]
        rear = sorted(rear)
    low_index = min(rear, key=lambda i: contour[i][1])
    high_index = max(rear, key=lambda i: contour[i][1])
    low_z = contour[low_index][1]
    high_z = contour[high_index][1]

    forward = _walk(contour, low_index, high_index, 1)
    reverse = _walk(contour, low_index, high_index, -1)

    def excursion(path: list[Point2]) -> float:
        return max((abs(point[0] - back_y) for point in path), default=0.0)

    front = forward if excursion(forward) >= excursion(reverse) else reverse
    if front and front[0][1] > front[-1][1]:
        front.reverse()

    front_sign = -outward_sign
    target = max(min_section_depth, minimum_web, 0.0)
    if target > 0 and len(front) > 2:
        reinforced = [front[0]]
        for depth, vertical in front[1:-1]:
            signed_depth = (depth - back_y) * front_sign
            if signed_depth < target:
                depth = back_y + front_sign * target
            reinforced.append((depth, vertical))
        reinforced.append(front[-1])
        front = reinforced

    centers = _tab_centers_along_edge(low_z, high_z, tab_length, tab_count)
    # Build rear path with one rectangular tab per centre (high → low).
    rear_path: list[Point2] = [(back_y, high_z)]
    tab_y = back_y + outward_sign * tab_depth
    clamped_centers: list[float] = []
    for center in sorted(centers, reverse=True):
        tab_high = center + tab_length / 2.0
        tab_low = center - tab_length / 2.0
        # Clamp into the edge so tabs never stick past the silhouette.
        tab_high = min(tab_high, high_z - 0.05)
        tab_low = max(tab_low, low_z + 0.05)
        if tab_high - tab_low < tab_length * 0.5:
            # Not enough room on this edge for a full tab; skip.
            continue
        # Re-centre after clamp so male/female stay concentric and full length.
        center = (tab_low + tab_high) / 2.0
        tab_high = center + tab_length / 2.0
        tab_low = center - tab_length / 2.0
        if tab_high > high_z - 0.02 or tab_low < low_z + 0.02:
            continue
        clamped_centers.append(center)
        rear_path.extend([
            (back_y, tab_high),
            (tab_y, tab_high),
            (tab_y, tab_low),
            (back_y, tab_low),
        ])
    rear_path.append((back_y, low_z))
    if not clamped_centers:
        # Fallback: one centred tab, possibly shorter, always inside the edge.
        center = (low_z + high_z) / 2.0
        half = min(tab_length / 2.0, max(0.5, (high_z - low_z) / 2.0 - 0.1))
        clamped_centers = [center]
        rear_path = [
            (back_y, high_z),
            (back_y, center + half),
            (tab_y, center + half),
            (tab_y, center - half),
            (back_y, center - half),
            (back_y, low_z),
        ]
    centers = sorted(clamped_centers)

    result = list(front) + rear_path[1:-1]
    cleaned: list[Point2] = []
    for point in result:
        if not cleaned or abs(point[0] - cleaned[-1][0]) > 1e-9 or abs(point[1] - cleaned[-1][1]) > 1e-9:
            cleaned.append(point)
    if len(cleaned) >= 3 and _polygon_area(cleaned) < 0:
        cleaned.reverse()
    return cleaned, centers


def apply_back_panel(job: SliceJob) -> BackPanel:
    """Add press-fit male tabs to every physical slice and build the female panel."""
    settings = job.settings
    male_length = settings.female_slot_length + settings.male_interference
    # Flat mounting plane sits at back_plane_offset along depth.
    # forward: material grows toward +Y from the plane; tabs point toward -Y (outward).
    offset = float(getattr(settings, "back_plane_offset", 0.0) or 0.0)
    if settings.depth_direction == "backward":
        back_y = offset
        outward_sign = 1.0
    elif settings.depth_direction == "forward":
        back_y = offset
        outward_sign = -1.0
    else:
        raise ValueError("Back-panel assembly requires one flat rear side; use forward or backward depth.")

    tab_count = max(1, int(settings.tab_count))
    slots: list[BackSlot] = []
    for part in job.parts:
        if not part.loops:
            continue
        outer_index = max(range(len(part.loops)), key=lambda index: abs(_polygon_area(part.loops[index])))
        contour, centers = _add_tabs(
            part.loops[outer_index],
            back_y=back_y,
            tab_depth=settings.connector_thickness,
            tab_length=male_length,
            outward_sign=outward_sign,
            minimum_web=settings.minimum_web,
            min_section_depth=settings.min_section_depth,
            tab_count=tab_count,
            force_flat_back=settings.force_flat_back,
        )
        part.loops[outer_index] = contour
        part.tab_centers = centers
        number = safe_number_placement(part.loops, min(settings.dimensions.height * 0.012 * getattr(settings, 'number_scale', 0.55) / 0.55, 4.0))
        part.number_position = number[0] if number else None
        part.number_height = number[1] if number else 1.2
        half_panel = settings.dimensions.height / 2.0
        half_slot = settings.female_slot_length / 2.0
        for center_z in centers:
            # Keep female slots fully inside the rectangular back panel.
            cz = max(-half_panel + half_slot + 0.05, min(half_panel - half_slot - 0.05, center_z))
            slots.append(BackSlot(
                part_name=part.name,
                center_x=part.x,
                center_z=cz,
                width=settings.connector_thickness,
                length=settings.female_slot_length,
            ))
            # Keep male tab centres matching the clamped female slots.
        part.tab_centers = [s.center_z for s in slots if s.part_name == part.name]

    panel = BackPanel(
        width=settings.dimensions.width,
        height=settings.dimensions.height,
        slots=slots,
    )
    job.back_panel = panel
    job.warnings.append(
        f"S1.1 back panel: {len(job.parts)} parts × {tab_count} tab(s); female "
        f"{settings.female_slot_length:.2f} × {settings.connector_thickness:.2f} mm, "
        f"male {male_length:.2f} × {settings.connector_thickness:.2f} mm (concentric)."
    )
    if settings.force_flat_back:
        job.warnings.append(
            f"Synthetic flat rear enforced; minimum section depth {settings.min_section_depth:.2f} mm; "
            f"plane offset {offset:.2f} mm."
        )
    if settings.minimum_web > 0:
        job.warnings.append(
            f"Structural guard: minimum {settings.minimum_web:.2f} mm rear-to-front web."
        )
    return panel
