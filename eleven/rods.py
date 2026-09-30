
from __future__ import annotations

import math
from dataclasses import dataclass

from .geometry import best_inside_point, edge_clearance, point_in_material
from .models import Point2, SliceJob


@dataclass(frozen=True)
class RodScore:
    position: Point2
    valid_count: int
    total_count: int
    minimum_clearance: float


def validate_position(job: SliceJob, position: Point2) -> RodScore:
    required = job.settings.edge_distance + job.settings.hole_diameter / 2.0
    clearances = []
    valid = 0
    for part in job.parts:
        if not part.loops or not point_in_material(position, part.loops):
            continue
        clearance = edge_clearance(position, part.loops)
        if clearance >= required:
            valid += 1
            clearances.append(clearance)
    return RodScore(position, valid, len(job.parts), min(clearances, default=0.0))


def _candidate_grid(job: SliceJob, steps: int = 21) -> list[Point2]:
    points = [p for part in job.parts for loop in part.loops for p in loop]
    if not points:
        return []
    min_y, max_y = min(p[0] for p in points), max(p[0] for p in points)
    min_z, max_z = min(p[1] for p in points), max(p[1] for p in points)
    return [
        (min_y + (max_y - min_y) * ix / (steps + 1), min_z + (max_z - min_z) * iz / (steps + 1))
        for ix in range(1, steps + 1)
        for iz in range(1, steps + 1)
    ]


def auto_place_rods(job: SliceJob) -> tuple[RodScore, RodScore] | None:
    scores = [validate_position(job, position) for position in _candidate_grid(job)]
    scores = [score for score in scores if score.valid_count > 0]
    if len(scores) < 2:
        return None
    scores.sort(key=lambda score: (score.valid_count, score.minimum_clearance), reverse=True)
    span = max(job.settings.dimensions.depth, job.settings.dimensions.height)
    minimum_separation = max(job.settings.hole_diameter * 3.0, span * 0.15)
    required_coverage = max(2, math.ceil(len(job.parts) * 0.60))
    required = job.settings.edge_distance + job.settings.hole_diameter / 2.0

    def covered_parts(position: Point2) -> set[int]:
        return {
            index for index, part in enumerate(job.parts)
            if point_in_material(position, part.loops) and edge_clearance(position, part.loops) >= required
        }

    candidates = [(score, covered_parts(score.position)) for score in scores[:120]]
    best_pair = None
    best_key = None
    for index, (first, first_covered) in enumerate(candidates):
        for second, second_covered in candidates[index + 1:]:
            separation = math.dist(first.position, second.position)
            if separation < minimum_separation:
                continue
            common = len(first_covered & second_covered)
            if common < required_coverage:
                continue
            key = (common, min(first.minimum_clearance, second.minimum_clearance), separation)
            if best_key is None or key > best_key:
                best_key = key
                best_pair = (first, second)
    if best_pair is None:
        job.rod_positions = []
        return None
    first, second = best_pair
    apply_rods(job, [first.position, second.position])
    return first, second


def apply_rods(job: SliceJob, positions: list[Point2]) -> list[RodScore]:
    job.rod_positions = positions[:2]
    scores = [validate_position(job, position) for position in job.rod_positions]
    required = job.settings.edge_distance + job.settings.hole_diameter / 2.0
    for part in job.parts:
        part.holes = [
            position for position in job.rod_positions
            if point_in_material(position, part.loops) and edge_clearance(position, part.loops) >= required
        ]
        if part.number_position and any(math.dist(part.number_position, hole) < job.settings.hole_diameter / 2 + 5.0 for hole in part.holes):
            safer_number = best_inside_point(
                part.loops,
                minimum_clearance=3.5,
                avoid=part.holes,
                avoid_distance=job.settings.hole_diameter / 2 + 5.0,
            )
            part.number_position = safer_number or best_inside_point(part.loops, minimum_clearance=0.0)
    return scores
