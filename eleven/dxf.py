
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

try:
    import ezdxf
except ImportError:  # The installer adds the proven v2.6.2 DXF engine.
    ezdxf = None

from .models import Point2, SliceJob, SlicePart


def _pair(code: int, value: object) -> str:
    if isinstance(value, float):
        rendered = f"{value:.8f}".rstrip("0").rstrip(".")
    else:
        rendered = str(value)
    return f"{code}\n{rendered}\n"


@dataclass
class DXFWriter:
    content: list[str]

    def add(self, code: int, value: object) -> None:
        self.content.append(_pair(code, value))

    def entity_start(self, kind: str, layer: str) -> None:
        self.add(0, kind)
        self.add(8, layer)

    def polyline(self, points: list[Point2], layer: str = "CUT") -> None:
        """Write an old-style closed POLYLINE for maximum Corel/CAD compatibility."""
        self.entity_start("POLYLINE", layer)
        self.add(66, 1)
        self.add(10, 0.0)
        self.add(20, 0.0)
        self.add(30, 0.0)
        self.add(70, 1)
        for x, y in points:
            self.entity_start("VERTEX", layer)
            self.add(10, x)
            self.add(20, y)
            self.add(30, 0.0)
        self.entity_start("SEQEND", layer)

    def lwpolyline(self, points: list[Point2], layer: str = "CUT") -> None:
        self.polyline(points, layer)

    def circle(self, center: Point2, radius: float, layer: str = "ROD_HOLE") -> None:
        self.entity_start("CIRCLE", layer)
        self.add(10, center[0])
        self.add(20, center[1])
        self.add(30, 0.0)
        self.add(40, radius)

    def text(self, value: str, position: Point2, height: float, layer: str = "PART_NUMBER") -> None:
        self.entity_start("TEXT", layer)
        self.add(10, position[0])
        self.add(20, position[1])
        self.add(30, 0.0)
        self.add(40, height)
        self.add(1, value)
        self.add(72, 1)
        self.add(73, 2)
        self.add(11, position[0])
        self.add(21, position[1])
        self.add(31, 0.0)

    def insert(self, name: str, position: Point2) -> None:
        self.entity_start("INSERT", "GUIDE")
        self.add(2, name)
        self.add(10, position[0])
        self.add(20, position[1])
        self.add(30, 0.0)

    def finish(self) -> str:
        return "".join(self.content)


def _header(writer: DXFWriter) -> None:
    writer.add(0, "SECTION")
    writer.add(2, "HEADER")
    writer.add(9, "$ACADVER")
    writer.add(1, "AC1009")
    writer.add(9, "$INSUNITS")
    writer.add(70, 4)  # millimetres
    writer.add(0, "ENDSEC")
    writer.add(0, "SECTION")
    writer.add(2, "TABLES")
    writer.add(0, "TABLE")
    writer.add(2, "LAYER")
    writer.add(70, 4)
    for name, color in (("CUT", 7), ("ROD_HOLE", 4), ("PART_NUMBER", 5), ("GUIDE", 8)):
        writer.add(0, "LAYER")
        writer.add(2, name)
        writer.add(70, 0)
        writer.add(62, color)
        writer.add(6, "CONTINUOUS")
    writer.add(0, "ENDTAB")
    writer.add(0, "ENDSEC")


def _block_name(part: SlicePart) -> str:
    return f"PART_{part.name}"


def _blocks(writer: DXFWriter, job: SliceJob) -> None:
    writer.add(0, "SECTION")
    writer.add(2, "BLOCKS")
    for part in job.parts:
        writer.add(0, "BLOCK")
        writer.add(8, "GUIDE")
        writer.add(2, _block_name(part))
        writer.add(70, 0)
        writer.add(10, 0.0)
        writer.add(20, 0.0)
        writer.add(30, 0.0)
        writer.add(3, _block_name(part))
        writer.add(1, "")
        for loop in part.loops:
            writer.lwpolyline(loop, "CUT")
        for hole in part.holes:
            writer.circle(hole, job.settings.hole_diameter / 2.0)
        if part.number_position:
            writer.text(part.name, part.number_position, part.number_height)
        writer.add(0, "ENDBLK")
        writer.add(8, "GUIDE")
    writer.add(0, "ENDSEC")


def _part_bounds(part: SlicePart) -> tuple[float, float, float, float]:
    points = [point for loop in part.loops for point in loop]
    if not points:
        return 0.0, 0.0, 0.0, 0.0
    return min(p[0] for p in points), min(p[1] for p in points), max(p[0] for p in points), max(p[1] for p in points)


def render_dxf(job: SliceJob, stack_check: bool = False) -> str:
    writer = DXFWriter([])
    _header(writer)
    writer.add(0, "SECTION")
    writer.add(2, "ENTITIES")
    cursor = 0.0
    margin = max(10.0, job.settings.dimensions.height * 0.05)
    for part in job.parts:
        if stack_check:
            position = (0.0, 0.0)
        else:
            min_x, _, max_x, _ = _part_bounds(part)
            position = (cursor - min_x, 0.0)
            cursor += max_x - min_x + margin
        offset_x, offset_y = position
        for loop in part.loops:
            writer.polyline([(x + offset_x, y + offset_y) for x, y in loop], "CUT")
        for hole in part.holes:
            writer.circle((hole[0] + offset_x, hole[1] + offset_y), job.settings.hole_diameter / 2.0)
        if part.number_position:
            height = max(1.2, min(job.settings.dimensions.height * 0.012 * getattr(job.settings, 'number_scale', 0.55) / 0.55, 4.0))
            writer.text(
                part.name,
                (part.number_position[0] + offset_x, part.number_position[1] + offset_y),
                height,
            )
    writer.add(0, "ENDSEC")
    writer.add(0, "EOF")
    return writer.finish()


def export_dxf(job: SliceJob, directory: str | Path) -> tuple[Path, Path, Path, Path | None]:
    destination = Path(directory)
    destination.mkdir(parents=True, exist_ok=True)
    parts_path = destination / "S1_CUT_PARTS.dxf"
    panel_path = destination / "S1_BACK_PANEL.dxf"
    stack_path = destination / "STACK_CHECK.dxf"
    wave_path: Path | None = None
    _write_ezdxf(job, parts_path, stack_check=False)
    _write_back_panel(job, panel_path)
    _write_ezdxf(job, stack_path, stack_check=True)
    wave_set = getattr(job, "wave_panels", None)
    if wave_set is not None and getattr(wave_set, "panels", None):
        wave_path = destination / "S1_WAVE_PANELS.dxf"
        _write_wave_panels(wave_set, wave_path)
    return parts_path, panel_path, stack_path, wave_path


def _write_back_panel(job: SliceJob, path: Path) -> None:
    if ezdxf is None:
        raise RuntimeError("ezdxf is not installed; run INSTALL_FIRST.bat once.")
    if job.back_panel is None:
        raise RuntimeError("Back panel geometry is missing.")
    panel = job.back_panel
    document = ezdxf.new("R2010")
    document.units = 4
    for name, color in (("PANEL_CUT", 1), ("SLOT_CUT", 3), ("SLOT_NUMBER", 5)):
        if name not in document.layers:
            document.layers.add(name=name, color=color)
    modelspace = document.modelspace()
    half_width = panel.width / 2.0
    half_height = panel.height / 2.0
    modelspace.add_lwpolyline([
        (-half_width, -half_height),
        (half_width, -half_height),
        (half_width, half_height),
        (-half_width, half_height),
    ], close=True, dxfattribs={"layer": "PANEL_CUT"})
    label_height = max(1.8, min(panel.height * 0.012, 5.0))
    for slot in panel.slots:
        half_slot_width = slot.width / 2.0
        half_slot_length = slot.length / 2.0
        modelspace.add_lwpolyline([
            (slot.center_x - half_slot_width, slot.center_z - half_slot_length),
            (slot.center_x + half_slot_width, slot.center_z - half_slot_length),
            (slot.center_x + half_slot_width, slot.center_z + half_slot_length),
            (slot.center_x - half_slot_width, slot.center_z + half_slot_length),
        ], close=True, dxfattribs={"layer": "SLOT_CUT"})
        label = modelspace.add_text(slot.part_name, height=label_height, dxfattribs={"layer": "SLOT_NUMBER"})
        label.dxf.insert = (slot.center_x, slot.center_z + half_slot_length + label_height * 0.8)
    document.saveas(path)


def _write_ezdxf(job: SliceJob, path: Path, stack_check: bool) -> None:
    """Use the same ezdxf/R2010 export route proven in v2.6.2."""
    if ezdxf is None:
        raise RuntimeError("ezdxf is not installed; run INSTALL_FIRST.bat once.")
    document = ezdxf.new("R2010")
    document.units = 4
    for name, color in (("CUT", 1), ("ROD_HOLE", 6), ("PART_NUMBER", 5)):
        if name not in document.layers:
            document.layers.add(name=name, color=color)
    modelspace = document.modelspace()
    cursor = 0.0
    margin = max(10.0, job.settings.dimensions.height * 0.05)
    for part in job.parts:
        if stack_check:
            offset_x, offset_y = 0.0, 0.0
        else:
            minimum_x, _, maximum_x, _ = _part_bounds(part)
            offset_x, offset_y = cursor - minimum_x, 0.0
            cursor += maximum_x - minimum_x + margin
        for loop in part.loops:
            if len(loop) >= 3:
                modelspace.add_lwpolyline(
                    [(x + offset_x, y + offset_y) for x, y in loop],
                    close=True,
                    dxfattribs={"layer": "CUT"},
                )
        for hole in part.holes:
            modelspace.add_circle(
                (hole[0] + offset_x, hole[1] + offset_y),
                job.settings.hole_diameter / 2.0,
                dxfattribs={"layer": "ROD_HOLE"},
            )
        if part.number_position:
            text = modelspace.add_text(
                part.name,
                height=part.number_height,
                dxfattribs={"layer": "PART_NUMBER"},
            )
            text.dxf.insert = (
                part.number_position[0] + offset_x,
                part.number_position[1] + offset_y,
            )
    document.saveas(path)



def _write_wave_panels(wave_set, path: Path) -> None:
    """Export decorative wavy panels only — main model file stays pure."""
    if ezdxf is None:
        raise RuntimeError("ezdxf is not installed; run INSTALL_FIRST.bat once.")
    document = ezdxf.new("R2010")
    document.units = 4
    for name, color in (("WAVE_CUT", 3), ("WAVE_NUMBER", 5)):
        if name not in document.layers:
            document.layers.add(name=name, color=color)
    modelspace = document.modelspace()
    cursor = 0.0
    margin = 12.0
    for panel in wave_set.panels:
        # Nest panels side by side for laser sheet layout
        xs = [p[0] for loop in panel.loops for p in loop]
        if not xs:
            continue
        min_x = min(xs)
        max_x = max(xs)
        offset_x = cursor - min_x
        for loop in panel.loops:
            if len(loop) >= 3:
                modelspace.add_lwpolyline(
                    [(x + offset_x, y) for x, y in loop],
                    close=True,
                    dxfattribs={"layer": "WAVE_CUT"},
                )
        # Label
        zs = [p[1] for loop in panel.loops for p in loop]
        mid_z = 0.5 * (min(zs) + max(zs)) if zs else 0.0
        label = modelspace.add_text(panel.name, height=2.0, dxfattribs={"layer": "WAVE_NUMBER"})
        label.dxf.insert = (offset_x + (max_x - min_x) * 0.5, mid_z)
        cursor += (max_x - min_x) + margin
    document.saveas(path)

def basic_dxf_check(text: str, expected_parts: int) -> list[str]:
    errors = []
    if not text.rstrip().endswith("EOF"):
        errors.append("DXF does not end with EOF.")
    if "\nENTITIES\n" not in text:
        errors.append("DXF has no ENTITIES section.")
    if text.count("\nPOLYLINE\n") < expected_parts:
        errors.append("DXF has fewer cut contours than requested parts.")
    if "\nVERTEX\n" not in text or "\nSEQEND\n" not in text:
        errors.append("DXF has no usable CUT geometry.")
    return errors
