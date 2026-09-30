
import struct
import tempfile
import unittest
from pathlib import Path

from eleven.back_panel import apply_back_panel
from eleven.dxf import export_dxf
from eleven.geometry import slice_mesh
from eleven.models import Dimensions, Mesh, SliceSettings
from eleven.stl import read_stl
from eleven.validation import validate_job


def write_binary_stl(path: Path, triangles) -> None:
    path.write_bytes(
        b"\0" * 80
        + struct.pack("<I", len(triangles))
        + b"".join(
            struct.pack("<12fH", 0, 0, 0, *tri[0], *tri[1], *tri[2], 0)
            for tri in triangles
        )
    )


def box_mesh():
    pts = [(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0), (0, 0, 1), (1, 0, 1), (1, 1, 1), (0, 1, 1)]
    faces = [
        (0, 1, 2), (0, 2, 3), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4),
        (2, 3, 7), (2, 7, 6), (0, 3, 7), (0, 7, 4), (1, 2, 6), (1, 6, 5),
    ]
    return Mesh(triangles=[(pts[a], pts[b], pts[c]) for a, b, c in faces], fingerprint="box")


class CoreTests(unittest.TestCase):
    def test_multi_tab_and_flat_back(self):
        settings = SliceSettings(
            dimensions=Dimensions(100, 50, 30),
            count=5,
            thickness=2.6,
            connector_thickness=2.6,
            female_slot_length=20,
            male_interference=0.25,
            tab_count=2,
            min_section_depth=15,
            force_flat_back=True,
            minimum_web=4,
        )
        job = slice_mesh(box_mesh(), settings)
        panel = apply_back_panel(job)
        self.assertEqual(len(job.parts), 5)
        for part in job.parts:
            self.assertEqual(len(part.tab_centers), 2)
        self.assertEqual(len(panel.slots), 10)
        self.assertAlmostEqual(settings.female_slot_length + settings.male_interference, 20.25)
        v = validate_job(job)
        self.assertTrue(v.checks.get("ALL TABS"))
        self.assertTrue(v.checks.get("CENTERED TAB SLOT"))
        with tempfile.TemporaryDirectory() as td:
            paths = export_dxf(job, td)
            self.assertTrue(all(p.stat().st_size > 500 for p in paths))

    def test_open_relief_synthetic_back(self):
        vertices = []
        for x in (0.0, 5.0, 10.0):
            vertices.extend([(x, 1.0, 0.0), (x, 4.0, 5.0), (x, 2.0, 10.0)])
        faces = []
        for column in range(2):
            a, b = column * 3, (column + 1) * 3
            faces.extend([(a, b, b + 1), (a, b + 1, a + 1), (a + 1, b + 1, b + 2), (a + 1, b + 2, a + 2)])
        path = Path(tempfile.mkdtemp()) / "open.stl"
        write_binary_stl(path, [tuple(vertices[i] for i in face) for face in faces])
        relief = read_stl(path)
        settings = SliceSettings(
            Dimensions(height=100, width=80, depth=30),
            count=7,
            force_flat_back=True,
            min_section_depth=15,
            tab_count=1,
        )
        job = slice_mesh(relief, settings)
        panel = apply_back_panel(job)
        self.assertGreaterEqual(len(job.parts), 2)
        self.assertEqual(len(panel.slots), len(job.parts))


if __name__ == "__main__":
    unittest.main()
