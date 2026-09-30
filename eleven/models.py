
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

Point2 = tuple[float, float]
Point3 = tuple[float, float, float]
Triangle = tuple[Point3, Point3, Point3]


@dataclass(frozen=True)
class Bounds3D:
    minimum: Point3
    maximum: Point3

    @property
    def size(self) -> Point3:
        return tuple(self.maximum[i] - self.minimum[i] for i in range(3))  # type: ignore[return-value]


@dataclass
class Mesh:
    triangles: list[Triangle]
    source: Path | None = None
    fingerprint: str = ""
    watertight: bool = False

    @property
    def bounds(self) -> Bounds3D:
        if not self.triangles:
            return Bounds3D((0.0, 0.0, 0.0), (0.0, 0.0, 0.0))
        points = [p for tri in self.triangles for p in tri]
        return Bounds3D(
            tuple(min(p[i] for p in points) for i in range(3)),  # type: ignore[arg-type]
            tuple(max(p[i] for p in points) for i in range(3)),  # type: ignore[arg-type]
        )

    @property
    def faces(self) -> int:
        return len(self.triangles)


@dataclass(frozen=True)
class Dimensions:
    height: float
    width: float
    depth: float

    def validate(self) -> None:
        if min(self.height, self.width, self.depth) <= 0:
            raise ValueError("All final dimensions must be greater than zero.")


@dataclass(frozen=True)
class SliceSettings:
    dimensions: Dimensions
    count: int
    thickness: float = 3.0
    gap: float = 0.0
    hole_diameter: float = 2.4
    edge_distance: float = 2.0
    simplify_tolerance: float = 0.08
    depth_direction: str = "forward"
    connector_thickness: float = 2.6
    female_slot_length: float = 20.0
    male_interference: float = 0.25
    minimum_web: float = 4.0
    tab_count: int = 1
    min_section_depth: float = 15.0
    force_flat_back: bool = True
    back_plane_offset: float = 0.0
    # S1.1.9 — compact labels + optional crop box + bold outline offset
    number_scale: float = 0.55
    crop_enabled: bool = False
    crop_width_min: float = 0.0   # fraction 0..1 of final width (slice axis)
    crop_width_max: float = 1.0
    crop_height_min: float = 0.0  # fraction 0..1 of final height
    crop_height_max: float = 1.0
    crop_depth_min: float = 0.0   # fraction 0..1 of final depth
    crop_depth_max: float = 1.0
    bold_enabled: bool = False
    bold_offset: float = 0.4      # mm outward expansion of outer cut contour
    # S1.1.10 — optional wavy ambient panels (do not alter main contours)
    wave_enabled: bool = False
    wave_amplitude: float = 8.0       # mm
    wave_wavelength: float = 40.0     # mm
    wave_phase_deg: float = 0.0       # degrees — جابه‌جایی فاز موج
    wave_panel_count: int = 2
    wave_strip_width: float = 18.0    # mm panel thickness in depth direction
    wave_gap: float = 4.0             # mm clearance from main model
    wave_placement: str = "sides"     # sides|left|right|both|behind|frame
    wave_offset_y: float = 0.0        # mm shift in depth
    wave_offset_z: float = 0.0        # mm shift in height
    wave_height_min: float = 0.0      # 0..1 coverage of model height
    wave_height_max: float = 1.0

    def validate(self) -> None:
        self.dimensions.validate()
        if self.count < 2:
            raise ValueError("Slice count must be at least 2.")
        if self.thickness <= 0 or self.hole_diameter <= 0:
            raise ValueError("Thickness and hole diameter must be greater than zero.")
        if self.connector_thickness <= 0 or self.female_slot_length <= 0:
            raise ValueError("Connector thickness and slot length must be greater than zero.")
        if self.male_interference < 0:
            raise ValueError("Male interference cannot be negative.")
        if self.minimum_web < 0:
            raise ValueError("Minimum structural web cannot be negative.")
        if self.tab_count < 1:
            raise ValueError("Tab count must be at least 1.")
        if self.min_section_depth < 0:
            raise ValueError("Minimum section depth cannot be negative.")
        # offset may be positive (into model) or slightly negative (behind model)
        if self.gap < 0 or self.edge_distance < 0 or self.simplify_tolerance < 0:
            raise ValueError("Gap, edge distance and tolerance cannot be negative.")
        if not (0.0 <= self.crop_width_min < self.crop_width_max <= 1.0):
            raise ValueError("Crop width range must be 0 ≤ min < max ≤ 1.")
        if not (0.0 <= self.crop_height_min < self.crop_height_max <= 1.0):
            raise ValueError("Crop height range must be 0 ≤ min < max ≤ 1.")
        if not (0.0 <= self.crop_depth_min < self.crop_depth_max <= 1.0):
            raise ValueError("Crop depth range must be 0 ≤ min < max ≤ 1.")
        if self.bold_offset < 0:
            raise ValueError("Bold offset cannot be negative.")
        if self.number_scale <= 0 or self.number_scale > 2.0:
            raise ValueError("Number scale must be between 0 and 2.")
        if self.wave_amplitude < 0:
            raise ValueError("Wave amplitude cannot be negative.")
        if self.wave_wavelength <= 0:
            raise ValueError("Wave wavelength must be positive.")
        if self.wave_panel_count < 1:
            raise ValueError("Wave panel count must be at least 1.")
        if self.wave_strip_width <= 0:
            raise ValueError("Wave strip width must be positive.")
        if self.wave_gap < 0:
            raise ValueError("Wave gap cannot be negative.")
        if self.wave_placement not in ("sides", "left", "right", "both", "behind", "frame"):
            raise ValueError("Wave placement must be sides/left/right/both/behind/frame.")


@dataclass
class SlicePart:
    index: int
    x: float
    loops: list[list[Point2]]
    holes: list[Point2] = field(default_factory=list)
    number_position: Point2 | None = None
    number_height: float = 1.4
    tab_centers: list[float] = field(default_factory=list)

    @property
    def name(self) -> str:
        wave = getattr(self, "_wave_name", None)
        if wave:
            return str(wave)
        # S1.1.9+: no leading "A" — shorter labels, less overlap on small parts
        return f"{self.index:02d}"


@dataclass
class SliceJob:
    parts: list[SlicePart]
    settings: SliceSettings
    source_fingerprint: str
    rod_positions: list[Point2] = field(default_factory=list)
    back_panel: "BackPanel | None" = None
    warnings: list[str] = field(default_factory=list)
    wave_panels: object | None = None  # WavePanelSet | None — set by apply step

    def nonempty_parts(self) -> Iterable[SlicePart]:
        return (part for part in self.parts if part.loops)


@dataclass(frozen=True)
class BackSlot:
    part_name: str
    center_x: float
    center_z: float
    width: float
    length: float


@dataclass
class BackPanel:
    width: float
    height: float
    slots: list[BackSlot] = field(default_factory=list)


@dataclass
class ValidationResult:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    checks: dict[str, bool] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.errors and all(self.checks.values())
