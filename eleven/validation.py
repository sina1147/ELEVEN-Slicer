
from __future__ import annotations

import math

from .dxf import basic_dxf_check, render_dxf
from .geometry import edge_clearance, point_in_material
from .models import SliceJob, ValidationResult


def validate_job(job: SliceJob) -> ValidationResult:
    result = ValidationResult()
    expected = job.settings.count
    result.checks["NO MIRROR"] = True
    tolerance = max(job.settings.dimensions.height, job.settings.dimensions.width, job.settings.dimensions.depth) * 0.005 + 0.05
    points = [point for part in job.parts for loop in part.loops for point in loop]
    plane_positions = [part.x for part in job.parts]
    plane_span_ok = (
        bool(plane_positions)
        and plane_positions == sorted(plane_positions)
        and all(-job.settings.dimensions.width / 2 - tolerance <= value <= job.settings.dimensions.width / 2 + tolerance for value in plane_positions)
    )
    z_ok = all(-job.settings.dimensions.height / 2 - tolerance <= p[1] <= job.settings.dimensions.height / 2 + tolerance for p in points)
    if job.settings.depth_direction == "forward":
        y_ok = all(-job.settings.connector_thickness - tolerance <= p[0] <= job.settings.dimensions.depth + max(job.settings.min_section_depth, 0) + tolerance for p in points)
    elif job.settings.depth_direction == "backward":
        y_ok = all(-job.settings.dimensions.depth - max(job.settings.min_section_depth, 0) - tolerance <= p[0] <= job.settings.connector_thickness + tolerance for p in points)
    else:
        y_ok = all(-job.settings.dimensions.depth / 2 - tolerance <= p[0] <= job.settings.dimensions.depth / 2 + tolerance for p in points)
    result.checks["NO SCALE ERROR"] = plane_span_ok and y_ok and z_ok
    result.checks["NO LOST PART"] = len(job.parts) >= max(2, math.ceil(expected * 0.30)) and all(part.loops for part in job.parts)
    result.checks["NO LOST NUMBER"] = all(
        part.number_position is not None and point_in_material(part.number_position, part.loops)
        for part in job.parts if part.loops
    )
    result.checks["NUMBER OFF CUT"] = all(
        part.number_position is not None and edge_clearance(part.number_position, part.loops) >= max(0.5, part.number_height * 0.2)
        for part in job.parts if part.loops
    )
    result.checks["CORRECT PART ORDER"] = [part.index for part in job.parts] == list(range(1, len(job.parts) + 1))
    if job.back_panel is not None:
        slots_by_part: dict[str, list] = {}
        for slot in job.back_panel.slots:
            slots_by_part.setdefault(slot.part_name, []).append(slot)
        # Every physical part must have at least one tab; shorter slices may carry fewer
        # than the requested tab_count when the rear edge cannot fit more.
        result.checks["ALL TABS"] = (
            all(len(part.tab_centers) >= 1 and part.name in slots_by_part for part in job.parts)
            and all(len(slots_by_part.get(part.name, [])) == len(part.tab_centers) for part in job.parts)
        )
        centered = True
        for part in job.parts:
            part_slots = sorted(slots_by_part.get(part.name, []), key=lambda s: s.center_z)
            centers = sorted(part.tab_centers)
            if len(part_slots) != len(centers):
                centered = False
                break
            for slot, cz in zip(part_slots, centers):
                if abs(slot.center_x - part.x) > 1e-6 or abs(slot.center_z - cz) > 1e-4:
                    centered = False
                    break
        result.checks["CENTERED TAB SLOT"] = centered
        result.checks["VALID BACK PANEL"] = all(
            abs(slot.center_x) + slot.width / 2.0 <= job.back_panel.width / 2.0 + tolerance
            and abs(slot.center_z) + slot.length / 2.0 <= job.back_panel.height / 2.0 + tolerance
            and abs(slot.width - job.settings.connector_thickness) <= 1e-8
            and abs(slot.length - job.settings.female_slot_length) <= 1e-8
            for slot in job.back_panel.slots
        )
    else:
        required = job.settings.edge_distance + job.settings.hole_diameter / 2.0
        holes_valid = all(
            point_in_material(hole, part.loops) and edge_clearance(hole, part.loops) >= required
            for part in job.parts for hole in part.holes
        )
        alignment_valid = all(hole in job.rod_positions for part in job.parts for hole in part.holes)
        result.checks["NO LOST HOLE"] = bool(job.rod_positions) and any(part.holes for part in job.parts)
        result.checks["VALID HOLE ALIGNMENT"] = holes_valid and alignment_valid
    dxf_errors = basic_dxf_check(render_dxf(job), len(job.parts))
    result.checks["VALID DXF"] = not dxf_errors
    for name, passed in result.checks.items():
        if not passed:
            result.errors.append(name)
    result.errors.extend(dxf_errors)
    result.warnings.extend(job.warnings)
    return result
