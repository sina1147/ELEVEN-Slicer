"""S1.1.10 — optional parametric wavy panels that complement the main model.

Main slice geometry is never modified. Waves are sized from the job bounds so
they stay in visual harmony, then can be shifted (phase / offset) freely.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .geometry import _polygon_area
from .models import Point2, SliceJob, SlicePart, SliceSettings


@dataclass
class WavePanel:
    """One decorative wavy strip ready for DXF export."""

    name: str
    loops: list[list[Point2]]
    # Placement hint for stacking / layout (mm, same space as part X after lay-flat)
    layout_x: float = 0.0


@dataclass
class WavePanelSet:
    panels: list[WavePanel] = field(default_factory=list)
    amplitude: float = 0.0
    wavelength: float = 0.0
    phase_deg: float = 0.0
    placement: str = "sides"


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _sine_wave_polyline(
    z0: float,
    z1: float,
    base_y: float,
    amplitude: float,
    wavelength: float,
    phase_rad: float,
    samples: int,
    outward: float = 1.0,
) -> list[Point2]:
    """Sample a sine wave along Z at depth base_y ± amplitude * outward."""
    if samples < 8:
        samples = 8
    span = z1 - z0
    if abs(span) < 1e-9 or wavelength <= 1e-6:
        return [(base_y, z0), (base_y, z1)]
    pts: list[Point2] = []
    for i in range(samples + 1):
        t = i / samples
        z = z0 + span * t
        y = base_y + outward * amplitude * math.sin(2.0 * math.pi * (z - z0) / wavelength + phase_rad)
        pts.append((y, z))
    return pts


def _closed_wave_strip(
    z0: float,
    z1: float,
    y_inner: float,
    y_outer: float,
    amplitude: float,
    wavelength: float,
    phase_rad: float,
    samples: int,
) -> list[Point2]:
    """Closed contour: outer sine edge + inner sine (or straight) edge.

    Outer edge carries the visible wave; inner edge is a calmer parallel wave
    so the strip thickness stays roughly constant (modern panel look).
    """
    outer = _sine_wave_polyline(z0, z1, y_outer, amplitude, wavelength, phase_rad, samples, outward=1.0)
    # Phase-matched softer wave on the inner side (half amplitude) keeps harmony
    # without fighting the main model silhouette.
    inner_amp = amplitude * 0.35
    inner = _sine_wave_polyline(z1, z0, y_inner, inner_amp, wavelength, phase_rad, samples, outward=1.0)
    # Build closed ring: outer low→high, then inner high→low
    ring = list(outer) + list(inner)
    cleaned: list[Point2] = []
    for p in ring:
        if not cleaned or abs(p[0] - cleaned[-1][0]) > 1e-9 or abs(p[1] - cleaned[-1][1]) > 1e-9:
            cleaned.append(p)
    if len(cleaned) >= 3 and _polygon_area(cleaned) < 0:
        cleaned.reverse()
    return cleaned


def _job_bounds_yz(job: SliceJob) -> tuple[float, float, float, float]:
    """(ymin, ymax, zmin, zmax) over all part loops in slice space (Y=depth, Z=height)."""
    ys: list[float] = []
    zs: list[float] = []
    for part in job.parts:
        for loop in part.loops:
            for x, z in loop:
                ys.append(x)
                zs.append(z)
    if not ys:
        h = job.settings.dimensions.height
        d = job.settings.dimensions.depth
        return 0.0, d, -h / 2.0, h / 2.0
    return min(ys), max(ys), min(zs), max(zs)


def generate_wave_panels(job: SliceJob) -> WavePanelSet:
    """Build optional wavy panels. Does not mutate main parts."""
    settings = job.settings
    if not getattr(settings, "wave_enabled", False):
        return WavePanelSet()

    amplitude = max(0.0, float(getattr(settings, "wave_amplitude", 8.0)))
    wavelength = max(5.0, float(getattr(settings, "wave_wavelength", 40.0)))
    phase_deg = float(getattr(settings, "wave_phase_deg", 0.0))
    phase_rad = math.radians(phase_deg)
    placement = str(getattr(settings, "wave_placement", "sides") or "sides").lower()
    panel_count = max(1, min(12, int(getattr(settings, "wave_panel_count", 2))))
    strip_width = max(3.0, float(getattr(settings, "wave_strip_width", 18.0)))
    gap = max(0.0, float(getattr(settings, "wave_gap", 4.0)))
    # User offsets — جابه‌جایی آزاد نسبت به مدل اصلی
    offset_y = float(getattr(settings, "wave_offset_y", 0.0))
    offset_z = float(getattr(settings, "wave_offset_z", 0.0))
    # Vertical coverage relative to model (0..1 fractions of height span)
    z_min_f = _clamp(float(getattr(settings, "wave_height_min", 0.0)), 0.0, 1.0)
    z_max_f = _clamp(float(getattr(settings, "wave_height_max", 1.0)), 0.0, 1.0)
    if z_max_f <= z_min_f:
        z_min_f, z_max_f = 0.0, 1.0

    ymin, ymax, zmin, zmax = _job_bounds_yz(job)
    z_span = zmax - zmin
    z0 = zmin + z_span * z_min_f + offset_z
    z1 = zmin + z_span * z_max_f + offset_z
    if z1 - z0 < 5.0:
        z1 = z0 + 5.0

    samples = max(24, min(160, int((z1 - z0) / max(1.0, wavelength) * 16)))

    panels: list[WavePanel] = []

    def make_strip(name: str, y_inner: float, y_outer: float, layout_x: float) -> WavePanel:
        loop = _closed_wave_strip(
            z0, z1, y_inner + offset_y, y_outer + offset_y,
            amplitude, wavelength, phase_rad, samples,
        )
        return WavePanel(name=name, loops=[loop], layout_x=layout_x)

    # Layout cursor for DXF nesting (separate from model space Y)
    layout_cursor = 0.0
    layout_margin = max(8.0, strip_width * 0.5)

    if placement in ("sides", "left", "right", "both"):
        # Place strips outside the model's depth span so the main silhouette is untouched.
        left_outer = ymin - gap - strip_width
        left_inner = ymin - gap
        right_inner = ymax + gap
        right_outer = ymax + gap + strip_width

        want_left = placement in ("sides", "left", "both")
        want_right = placement in ("sides", "right", "both")

        # Multiple parallel strips per side for a richer modern stack
        per_side = max(1, (panel_count + 1) // 2) if want_left and want_right else panel_count
        idx = 1
        if want_left:
            for i in range(per_side):
                shift = i * (strip_width + gap * 0.5)
                panel = make_strip(
                    f"W{idx:02d}",
                    left_inner - shift,
                    left_outer - shift,
                    layout_cursor,
                )
                panels.append(panel)
                layout_cursor += strip_width + amplitude * 2 + layout_margin
                idx += 1
        if want_right:
            for i in range(per_side):
                shift = i * (strip_width + gap * 0.5)
                panel = make_strip(
                    f"W{idx:02d}",
                    right_inner + shift,
                    right_outer + shift,
                    layout_cursor,
                )
                panels.append(panel)
                layout_cursor += strip_width + amplitude * 2 + layout_margin
                idx += 1

    elif placement == "behind":
        # Horizontal-ish waves behind the relief (still in YZ plane of each slice family)
        # One wide panel spanning model depth with wave along height
        y_mid = 0.5 * (ymin + ymax) + offset_y
        half = max(strip_width * 0.5, (ymax - ymin) * 0.15)
        for i in range(panel_count):
            phase_i = phase_rad + i * (math.pi / max(1, panel_count))
            loop = _closed_wave_strip(
                z0, z1,
                y_mid - half - i * (gap + 2),
                y_mid + half + i * (gap + 2),
                amplitude * (1.0 - 0.08 * i),
                wavelength,
                phase_i,
                samples,
            )
            panels.append(WavePanel(name=f"W{i+1:02d}", loops=[loop], layout_x=layout_cursor))
            layout_cursor += (half * 2 + amplitude * 2) + layout_margin

    elif placement == "frame":
        # Four-side soft frame: left + right strips (top/bottom as shorter waves)
        left_outer = ymin - gap - strip_width
        left_inner = ymin - gap
        right_inner = ymax + gap
        right_outer = ymax + gap + strip_width
        panels.append(make_strip("W01", left_inner, left_outer, layout_cursor))
        layout_cursor += strip_width + amplitude * 2 + layout_margin
        panels.append(make_strip("W02", right_inner, right_outer, layout_cursor))
        layout_cursor += strip_width + amplitude * 2 + layout_margin
        # Top & bottom: wave along depth (swap roles — sample along Y, constant Z band)
        # Represented as rectangles with wavy long edges in the same YZ plane.
        y0, y1 = ymin - gap, ymax + gap
        top_z = z1 + gap
        bot_z = z0 - gap
        for name, z_inner, z_outer in (("W03", top_z, top_z + strip_width), ("W04", bot_z - strip_width, bot_z)):
            # Wave along Y axis at fixed Z band
            samples_y = max(24, min(160, int((y1 - y0) / max(1.0, wavelength) * 16)))
            outer: list[Point2] = []
            for i in range(samples_y + 1):
                t = i / samples_y
                y = y0 + (y1 - y0) * t
                z = z_outer + amplitude * math.sin(2.0 * math.pi * (y - y0) / wavelength + phase_rad)
                outer.append((y + offset_y, z + offset_z))
            inner: list[Point2] = []
            for i in range(samples_y + 1):
                t = 1.0 - i / samples_y
                y = y0 + (y1 - y0) * t
                z = z_inner + amplitude * 0.35 * math.sin(2.0 * math.pi * (y - y0) / wavelength + phase_rad)
                inner.append((y + offset_y, z + offset_z))
            ring = outer + inner
            cleaned: list[Point2] = []
            for p in ring:
                if not cleaned or abs(p[0] - cleaned[-1][0]) > 1e-9 or abs(p[1] - cleaned[-1][1]) > 1e-9:
                    cleaned.append(p)
            if len(cleaned) >= 3 and _polygon_area(cleaned) < 0:
                cleaned.reverse()
            panels.append(WavePanel(name=name, loops=[cleaned], layout_x=layout_cursor))
            layout_cursor += strip_width + amplitude * 2 + layout_margin

    else:
        # Fallback: single right-side strip
        panels.append(make_strip("W01", ymax + gap, ymax + gap + strip_width, 0.0))

    result = WavePanelSet(
        panels=panels,
        amplitude=amplitude,
        wavelength=wavelength,
        phase_deg=phase_deg,
        placement=placement,
    )
    if panels:
        job.warnings.append(
            f"Wave panels: {len(panels)} strip(s) · amp {amplitude:.1f} mm · λ {wavelength:.1f} mm · "
            f"phase {phase_deg:.0f}° · placement={placement} (main model contours unchanged)."
        )
    return result


def wave_panels_as_parts(wave_set: WavePanelSet) -> list[SlicePart]:
    """Convert wave panels to SlicePart-like objects for shared DXF writer path."""
    parts: list[SlicePart] = []
    for i, panel in enumerate(wave_set.panels, start=1):
        part = SlicePart(
            index=900 + i,  # high index band so names stay W-prefixed via override
            x=panel.layout_x,
            loops=panel.loops,
            number_position=None,
            number_height=1.2,
        )
        # Override display name to W01… without touching main A/01 series
        object.__setattr__(part, "_wave_name", panel.name)
        parts.append(part)
    return parts
