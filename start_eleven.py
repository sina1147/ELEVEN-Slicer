
from __future__ import annotations

import json
import mimetypes
import os
import tempfile
import threading
import urllib.parse
import uuid
import webbrowser
import zipfile
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from eleven import __version__
from eleven.back_panel import apply_back_panel
from eleven.dxf import export_dxf
from eleven.geometry import TaskCancelled, slice_mesh, slice_count_from_quality
from eleven.models import Dimensions, Mesh, SliceJob, SliceSettings
from eleven.stl import read_stl
from eleven.validation import validate_job
from eleven.wave_panels import generate_wave_panels


ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
WORK = Path(tempfile.mkdtemp(prefix="eleven_s1_"))
MODELS: dict[str, object] = {}
MODEL_PATHS: dict[str, Path] = {}
MODEL_AXES: dict[str, dict[str, str]] = {}
JOBS: dict[str, SliceJob] = {}
TASKS: dict[str, threading.Event] = {}
MAX_UPLOAD = 750 * 1024 * 1024

try:
    import numpy as np
    import trimesh
except ImportError:
    np = None
    trimesh = None


def isolate_primary_subject(path: Path, output: Path) -> tuple[Path, dict]:
    """Use the proven v2.6.2 dominant-component rule to remove detached marks."""
    if trimesh is None or np is None:
        return path, {"available": False, "removed": 0, "components": 0, "applied": False}
    loaded = trimesh.load(str(path), force="mesh", process=False)
    work = loaded.to_mesh() if isinstance(loaded, trimesh.Scene) else loaded
    if not isinstance(work, trimesh.Trimesh) or work.is_empty:
        return path, {"available": True, "removed": 0, "components": 0, "applied": False}
    work.merge_vertices()
    n_faces = int(len(work.faces))
    adjacency = np.asarray(work.face_adjacency, dtype=np.int64)
    if n_faces == 0 or len(adjacency) == 0:
        return path, {"available": True, "removed": 0, "components": 1, "applied": False}
    parent = np.arange(n_faces, dtype=np.int64)
    rank = np.zeros(n_faces, dtype=np.uint8)

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = int(parent[x])
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra == rb:
            return
        if rank[ra] < rank[rb]:
            parent[ra] = rb
        elif rank[ra] > rank[rb]:
            parent[rb] = ra
        else:
            parent[rb] = ra
            rank[ra] += 1

    for a, b in adjacency:
        union(int(a), int(b))
    roots = np.fromiter((find(i) for i in range(n_faces)), dtype=np.int64, count=n_faces)
    _, inverse, counts = np.unique(roots, return_inverse=True, return_counts=True)
    primary = int(np.argmax(counts))
    faces = np.flatnonzero(inverse == primary)
    ratio = float(len(faces)) / max(1, n_faces)
    components = int(len(counts))
    if components <= 1 or ratio < 0.70:
        return path, {"available": True, "removed": 0, "components": components, "applied": False, "kept_ratio": 1.0}
    subject = work.submesh([faces], append=True, repair=False)
    subject.remove_unreferenced_vertices()
    output.write_bytes(subject.export(file_type="stl"))
    return output, {
        "available": True, "removed": n_faces - int(len(subject.faces)),
        "components": components, "applied": True, "kept_ratio": ratio,
    }


def detect_semantic_axes(mesh: Mesh) -> tuple[dict[str, str], dict[str, float]]:
    sizes = mesh.bounds.size
    names = ("x", "y", "z")
    ordered = sorted(range(3), key=lambda i: sizes[i], reverse=True)
    height_i, width_i, depth_i = ordered[0], ordered[1], ordered[2]
    axes = {"height": names[height_i], "width": names[width_i], "depth": names[depth_i]}
    semantic_sizes = {"height": sizes[height_i], "width": sizes[width_i], "depth": sizes[depth_i]}
    return axes, semantic_sizes


def _parse_back_face(back_face: str, semantic_axes: dict[str, str]) -> tuple[str, str]:
    """Return (source_axis, side) where side is 'min' or 'max' of that axis.

    Accepts: front/back/left/right/top/bottom (semantic) or
    x_min/x_max/y_min/y_max/z_min/z_max (explicit source faces).
    """
    face = (back_face or "back").strip().lower().replace("-", "_").replace(" ", "_")
    explicit = {
        "x_min": ("x", "min"), "x_max": ("x", "max"),
        "y_min": ("y", "min"), "y_max": ("y", "max"),
        "z_min": ("z", "min"), "z_max": ("z", "max"),
        "neg_x": ("x", "min"), "pos_x": ("x", "max"),
        "neg_y": ("y", "min"), "pos_y": ("y", "max"),
        "neg_z": ("z", "min"), "pos_z": ("z", "max"),
    }
    if face in explicit:
        return explicit[face]
    # Semantic labels relative to height/width/depth roles of the model.
    # back = min depth, front = max depth, left = min width, right = max width,
    # bottom = min height, top = max height.
    mapping = {
        "back": (semantic_axes["depth"], "min"),
        "rear": (semantic_axes["depth"], "min"),
        "front": (semantic_axes["depth"], "max"),
        "left": (semantic_axes["width"], "min"),
        "right": (semantic_axes["width"], "max"),
        "bottom": (semantic_axes["height"], "min"),
        "top": (semantic_axes["height"], "max"),
        # Persian-friendly aliases already lowercased via caller
        "aqab": (semantic_axes["depth"], "min"),
        "jelo": (semantic_axes["depth"], "max"),
        "chap": (semantic_axes["width"], "min"),
        "rast": (semantic_axes["width"], "max"),
        "bala": (semantic_axes["height"], "max"),
        "paeen": (semantic_axes["height"], "min"),
    }
    if face in mapping:
        return mapping[face]
    return semantic_axes["depth"], "min"


def orient_for_slice(
    mesh: Mesh,
    semantic_axes: dict[str, str],
    slice_axis: str,
    back_face: str = "back",
) -> tuple[Mesh, tuple[str, str, str], str]:
    """Place slice axis on core X and chosen back face on core Y rear (y→0).

    Returns oriented mesh, core axis names (x,y,z source), and depth_direction
    ('forward' means material grows toward +Y from the flat rear at y=0).
    """
    names = ("x", "y", "z")
    source_index = {name: index for index, name in enumerate(names)}
    slice_axis = slice_axis if slice_axis in source_index else semantic_axes["width"]
    back_axis, back_side = _parse_back_face(back_face, semantic_axes)
    if back_axis not in source_index:
        back_axis = semantic_axes["depth"]
        back_side = "min"

    y_i = source_index[back_axis]
    x_i = source_index[slice_axis]
    if x_i == y_i:
        # Back face owns the depth axis. Pick another source axis for slicing
        # (prefer semantic width, then height, then any remaining).
        preferred = [semantic_axes["width"], semantic_axes["height"], semantic_axes["depth"], "x", "y", "z"]
        for candidate in preferred:
            ci = source_index[candidate]
            if ci != y_i:
                x_i = ci
                slice_axis = candidate
                break
    z_i = next(index for index in range(3) if index not in (x_i, y_i))

    permutation = [x_i, y_i, z_i]
    inversions = sum(permutation[i] > permutation[j] for i in range(3) for j in range(i + 1, 3))
    flip_x = inversions % 2 == 1
    # Flat rear at the chosen face: after mapping, that face sits at the low-Y
    # side of the source axis, then transform_mesh puts min Y at y=0 for forward.
    flip_y = back_side == "max"

    def convert(point):
        x = point[x_i]
        y = point[y_i]
        z = point[z_i]
        if flip_x:
            x = -x
        if flip_y:
            y = -y
        return (x, y, z)

    triangles = [tuple(convert(point) for point in triangle) for triangle in mesh.triangles]
    oriented = Mesh(
        triangles=triangles,
        source=mesh.source,
        fingerprint=mesh.fingerprint + f":slice-{slice_axis}:back-{back_axis}-{back_side}",
        watertight=mesh.watertight,
    )
    # Always "forward": material extends from flat rear (y=0) toward +depth.
    return oriented, (names[x_i], names[y_i], names[z_i]), "forward"


def _slice_axis_is_flipped(core_axes: tuple[str, str, str]) -> bool:
    source_index = {name: index for index, name in enumerate(("x", "y", "z"))}
    permutation = [source_index[name] for name in core_axes]
    inversions = sum(permutation[i] > permutation[j] for i in range(3) for j in range(i + 1, 3))
    return inversions % 2 == 1


def fa_error(exc: Exception) -> str:
    translations = {
        "All final dimensions must be greater than zero.": "همه ابعاد نهایی باید بزرگ‌تر از صفر باشند.",
        "Slice count must be at least 2.": "تعداد برش باید حداقل ۲ باشد.",
        "Thickness and hole diameter must be greater than zero.": "ضخامت و قطر سوراخ باید بزرگ‌تر از صفر باشند.",
        "ezdxf is not installed; run INSTALL_FIRST.bat once.": "موتور استاندارد DXF نصب نیست؛ یک‌بار INSTALL_FIRST.bat را اجرا کنید.",
        "A flat rear edge could not be found on every slice.": "لبهٔ صاف پشت روی همه مقاطع پیدا نشد؛ گزینه «صفحه پشت مصنوعی» را روشن کنید.",
        "A slice rear edge is shorter than the requested male tab.": "لبهٔ پشت بعضی مقاطع از طول خار نری کوتاه‌تر است؛ طول شیار یا تعداد خار را کم کنید.",
        "A slice contour is too small for a back-panel tab.": "یکی از مقاطع برای ساخت خار صفحهٔ پشت خیلی کوچک است.",
        "Back-panel assembly requires one flat rear side; use forward or backward depth.": "مونتاژ صفحهٔ پشت به یک سمت صاف نیاز دارد.",
        "Tab count must be at least 1.": "تعداد خار باید حداقل ۱ باشد.",
    }
    message = str(exc)
    if message.startswith("Only "):
        return (
            "برای این مدل با محور و تعداد برش فعلی، مقطع قابل‌برش کافی ساخته نشد. "
            "محور برش را عوض کنید، تعداد برش را کم کنید، یا مدل STL را طوری بفرستید که پشت آن صاف باشد."
        )
    return translations.get(message, message)


def recommend_settings(
    model_width: float,
    model_height: float,
    model_depth: float,
    sheet_w: float,
    sheet_h: float,
    desired_count: int,
    thickness: float,
    gap: float,
) -> dict:
    """Suggest count / scale trade-offs for sheet usage vs detail."""
    desired_count = max(2, int(desired_count))
    thickness = max(0.5, float(thickness))
    gap = max(0.0, float(gap))
    pitch = thickness + gap

    # Max parts that fit across the longer sheet side if each part is roughly square of model height.
    longer = max(sheet_w, sheet_h)
    shorter = min(sheet_w, sheet_h)

    def quality_for(count: int, scale: float) -> float:
        # Higher count + larger scale → more detail preserved.
        return count * scale

    suggestions = []

    # 1) Keep dimensions, find nearest count that still "fits" conceptually on sheet.
    #    Estimate nested length ≈ count * (depth*scale is irrelevant); use width span.
    for delta in range(-15, 16):
        c = desired_count + delta
        if c < 2:
            continue
        # Approximate: each slice is ~ height tall and depth wide on sheet after lay-flat.
        part_w = model_depth  # after slice, section is depth × height
        part_h = model_height
        cols = max(1, int(longer // (part_w + 5)))
        rows = max(1, int((c + cols - 1) // cols))
        used_h = rows * (part_h + 5)
        fits = used_h <= shorter * 1.15 or c * part_w <= longer * 1.5
        scale = 1.0
        suggestions.append({
            "kind": "count_near",
            "count": c,
            "height": round(model_height * scale, 2),
            "width": round(model_width * scale, 2),
            "depth": round(model_depth * scale, 2),
            "scale": scale,
            "quality": quality_for(c, scale),
            "sheet_fit": bool(fits),
            "note": "نزدیک به تعداد درخواستی؛ ابعاد ثابت",
        })

    # 2) Keep count, shrink scale so nested footprint drops (less sheet).
    for scale in (1.0, 0.95, 0.9, 0.85, 0.8, 0.75, 0.7):
        part_w = model_depth * scale
        part_h = model_height * scale
        cols = max(1, int(longer // (part_w + 5)))
        rows = max(1, int((desired_count + cols - 1) // cols))
        used_h = rows * (part_h + 5)
        fits = used_h <= shorter
        suggestions.append({
            "kind": "scale_down",
            "count": desired_count,
            "height": round(model_height * scale, 2),
            "width": round(model_width * scale, 2),
            "depth": round(model_depth * scale, 2),
            "scale": scale,
            "quality": quality_for(desired_count, scale),
            "sheet_fit": bool(fits),
            "note": f"تعداد ثابت؛ مقیاس {int(scale*100)}٪ برای مصرف ورق کمتر",
        })

    # 3) More parts for more detail at same overall size.
    for c in (desired_count, desired_count + 2, desired_count + 4, max(desired_count, int(model_width / max(pitch, 0.5)))):
        c = max(2, c)
        suggestions.append({
            "kind": "more_detail",
            "count": c,
            "height": round(model_height, 2),
            "width": round(model_width, 2),
            "depth": round(model_depth, 2),
            "scale": 1.0,
            "quality": quality_for(c, 1.0),
            "sheet_fit": True,
            "note": "جزئیات بیشتر با افزایش تعداد برش در ابعاد فعلی",
        })

    # Deduplicate by (count, scale) and rank.
    seen = set()
    unique = []
    for item in suggestions:
        key = (item["count"], item["scale"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    unique.sort(key=lambda s: (-s["sheet_fit"], -s["quality"], abs(s["count"] - desired_count)))
    best_detail = max(unique, key=lambda s: s["quality"])
    best_economy = min(
        (s for s in unique if s["scale"] < 1.0 or s["count"] <= desired_count),
        key=lambda s: (s["height"] * s["width"], -s["quality"]),
        default=unique[0],
    )
    nearest_count = min(unique, key=lambda s: (abs(s["count"] - desired_count), -s["quality"]))
    return {
        "nearest_count": nearest_count,
        "more_detail": best_detail,
        "less_sheet": best_economy,
        "options": unique[:12],
    }


class Handler(BaseHTTPRequestHandler):
    server_version = f"ELEVEN/{__version__}"

    def log_message(self, fmt: str, *args: object) -> None:
        print("[ELEVEN]", fmt % args)

    def send_json(self, payload: object, status: int = 200) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if length > 2_000_000:
            raise ValueError("درخواست بیش از اندازه بزرگ است.")
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/health":
            self.send_json({"ok": True, "version": __version__})
            return
        if parsed.path == "/api/export":
            self.export_job(urllib.parse.parse_qs(parsed.query).get("job", [""])[0])
            return
        if parsed.path == "/api/model":
            model_id = urllib.parse.parse_qs(parsed.query).get("id", [""])[0]
            target = MODEL_PATHS.get(model_id)
            if target is None or not target.is_file():
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            data = target.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "model/stl")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
            return
        relative = "index.html" if parsed.path == "/" else parsed.path.lstrip("/")
        target = (STATIC / relative).resolve()
        if STATIC.resolve() not in target.parents and target != STATIC.resolve():
            self.send_error(HTTPStatus.FORBIDDEN)
            return
        if not target.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        data = target.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", mimetypes.guess_type(target.name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        try:
            if parsed.path == "/api/upload":
                self.upload(urllib.parse.parse_qs(parsed.query).get("name", ["model.stl"])[0])
            elif parsed.path == "/api/process":
                self.process(self.read_json())
            elif parsed.path == "/api/recommend":
                self.recommend(self.read_json())
            elif parsed.path == "/api/cancel":
                token = str(self.read_json().get("token", ""))
                if token in TASKS:
                    TASKS[token].set()
                self.send_json({"ok": True})
            else:
                self.send_error(HTTPStatus.NOT_FOUND)
        except TaskCancelled:
            self.send_json({"error": "پردازش توسط کاربر متوقف شد."}, 499)
        except Exception as exc:
            self.send_json({"error": fa_error(exc)}, 400)

    def upload(self, filename: str) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > MAX_UPLOAD:
            raise ValueError("حجم فایل STL معتبر نیست یا بیش از حد مجاز است.")
        model_id = uuid.uuid4().hex
        path = WORK / f"{model_id}.stl"
        remaining = length
        with path.open("wb") as output:
            while remaining:
                chunk = self.rfile.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise ValueError("فایل STL ناقص دریافت شد.")
                output.write(chunk)
                remaining -= len(chunk)
        isolate = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("isolate", ["1"])[0] != "0"
        effective_path, isolation = (
            isolate_primary_subject(path, WORK / f"{model_id}_clean.stl")
            if isolate
            else (path, {"available": trimesh is not None, "removed": 0, "components": 0, "applied": False})
        )
        source_mesh = read_stl(effective_path)
        semantic_axes, semantic_sizes = detect_semantic_axes(source_mesh)
        MODELS[model_id] = source_mesh
        MODEL_AXES[model_id] = semantic_axes
        MODEL_PATHS[model_id] = effective_path
        sx, sy, sz = source_mesh.bounds.size
        self.send_json({
            "model_id": model_id,
            "filename": filename,
            "x": sx, "y": sy, "z": sz,
            "faces": source_mesh.faces,
            "watertight": source_mesh.watertight,
            "semantic_axes": semantic_axes,
            "height": semantic_sizes["height"],
            "width": semantic_sizes["width"],
            "depth": semantic_sizes["depth"],
            "isolation": isolation,
            "preview_url": f"/api/model?id={model_id}",
        })

    def recommend(self, payload: dict) -> None:
        result = recommend_settings(
            model_width=float(payload.get("width", 100)),
            model_height=float(payload.get("height", 100)),
            model_depth=float(payload.get("depth", 30)),
            sheet_w=float(payload.get("sheet_w", 600)),
            sheet_h=float(payload.get("sheet_h", 400)),
            desired_count=int(payload.get("count", 20)),
            thickness=float(payload.get("thickness", 2.6)),
            gap=float(payload.get("gap", 0.0)),
        )
        self.send_json({"ok": True, **result})

    def process(self, payload: dict) -> None:
        model_id = str(payload.get("model_id", ""))
        source_mesh = MODELS.get(model_id)
        semantic_axes = MODEL_AXES.get(model_id)
        # Survive server restarts / GC: reload mesh from the upload path.
        if source_mesh is None and model_id in MODEL_PATHS:
            path = MODEL_PATHS[model_id]
            if path and Path(path).exists():
                source_mesh = read_stl(path)
                MODELS[model_id] = source_mesh
                if semantic_axes is None:
                    semantic_axes, _ = detect_semantic_axes(source_mesh)
                    MODEL_AXES[model_id] = semantic_axes
        if source_mesh is None or semantic_axes is None:
            raise ValueError("مدل در حافظه نیست؛ همان فایل STL را یک‌بار دیگر باز کنید (تنظیمات شما پاک نمی‌شود اگر صفحه را نبندید).")
        # Only the axis the user picks — never auto-switch.
        slice_axis = str(payload.get("slice_axis", semantic_axes["width"])).lower()
        if slice_axis not in ("x", "y", "z"):
            slice_axis = semantic_axes["width"]
        back_face = str(payload.get("back_face", payload.get("depth_direction", "back")))
        # Map legacy forward/backward to semantic back/front
        if back_face in ("forward",):
            back_face = "back"
        elif back_face in ("backward",):
            back_face = "front"
        mesh, core_axes, depth_direction = orient_for_slice(
            source_mesh, semantic_axes, slice_axis, back_face=back_face
        )
        # core_axes[0] is the actual slice axis after resolving conflicts with back face.
        slice_axis = core_axes[0]
        semantic_targets = {
            "height": float(payload["height"]),
            "width": float(payload["width"]),
            "depth": float(payload["depth"]),
        }
        target_by_source = {semantic_axes[role]: value for role, value in semantic_targets.items()}
        token = str(payload.get("token") or uuid.uuid4().hex)
        event = threading.Event()
        TASKS[token] = event
        # Depth axis is the chosen back-face axis (core Y). Use that source size
        # for the depth dimension so left/right backs scale correctly.
        settings = SliceSettings(
            dimensions=Dimensions(
                height=target_by_source.get(core_axes[2], semantic_targets["height"]),
                width=target_by_source.get(core_axes[0], semantic_targets["width"]),
                depth=target_by_source.get(core_axes[1], semantic_targets["depth"]),
            ),
            count=slice_count_from_quality(
                width=target_by_source.get(core_axes[0], semantic_targets["width"]),
                thickness=float(payload.get("thickness", 2.6)),
                gap=float(payload.get("gap", 0.0)),
                quality=str(payload.get("quality", "medium")),
            ),
            thickness=float(payload.get("thickness", 2.6)),
            gap=float(payload.get("gap", 0.0)),
            hole_diameter=float(payload.get("hole_diameter", 2.4)),
            edge_distance=float(payload.get("edge_distance", 2.0)),
            simplify_tolerance=float(payload.get("simplify_tolerance", 0.12)),
            depth_direction=depth_direction,
            connector_thickness=float(payload.get("connector_thickness", payload.get("thickness", 2.6))),
            female_slot_length=float(payload.get("female_slot_length", 20.0)),
            male_interference=float(payload.get("male_interference", 0.25)),
            minimum_web=float(payload.get("minimum_web", 4.0)),
            tab_count=max(1, int(payload.get("tab_count", 1))),
            min_section_depth=float(payload.get("min_section_depth", 15.0)),
            # Always build a frame-like flat rear so open models still get tabs + panel.
            force_flat_back=bool(payload.get("force_flat_back", True)),
            back_plane_offset=float(payload.get("back_plane_offset", 0.0)),
            number_scale=float(payload.get("number_scale", 0.55)),
            crop_enabled=bool(payload.get("crop_enabled", False)),
            crop_width_min=float(payload.get("crop_width_min", 0.0)),
            crop_width_max=float(payload.get("crop_width_max", 1.0)),
            crop_height_min=float(payload.get("crop_height_min", 0.0)),
            crop_height_max=float(payload.get("crop_height_max", 1.0)),
            crop_depth_min=float(payload.get("crop_depth_min", 0.0)),
            crop_depth_max=float(payload.get("crop_depth_max", 1.0)),
            bold_enabled=bool(payload.get("bold_enabled", False)),
            bold_offset=float(payload.get("bold_offset", 0.4)),
            wave_enabled=bool(payload.get("wave_enabled", False)),
            wave_amplitude=float(payload.get("wave_amplitude", 8.0)),
            wave_wavelength=float(payload.get("wave_wavelength", 40.0)),
            wave_phase_deg=float(payload.get("wave_phase_deg", 0.0)),
            wave_panel_count=max(1, int(payload.get("wave_panel_count", 2))),
            wave_strip_width=float(payload.get("wave_strip_width", 18.0)),
            wave_gap=float(payload.get("wave_gap", 4.0)),
            wave_placement=str(payload.get("wave_placement", "sides")),
            wave_offset_y=float(payload.get("wave_offset_y", 0.0)),
            wave_offset_z=float(payload.get("wave_offset_z", 0.0)),
            wave_height_min=float(payload.get("wave_height_min", 0.0)),
            wave_height_max=float(payload.get("wave_height_max", 1.0)),
        )
        try:
            job = slice_mesh(mesh, settings, event)
            panel = apply_back_panel(job)
            job.wave_panels = generate_wave_panels(job)
            job_id = uuid.uuid4().hex
            JOBS[job_id] = job
            validation = validate_job(job) if bool(payload.get("final")) else None
            self.send_json({
                "ok": True,
                "job_id": job_id,
                "part_count": len(job.parts),
                "requested_part_count": settings.count,
                "slice_axis_used": slice_axis,
                "back_face": back_face,
                "slice_positions": [
                    round((-part.x if _slice_axis_is_flipped(core_axes) else part.x), 6)
                    for part in job.parts
                ],
                "back_panel": {
                    "width": panel.width,
                    "height": panel.height,
                    "female_slot_length": settings.female_slot_length,
                    "male_tab_length": settings.female_slot_length + settings.male_interference,
                    "connector_thickness": settings.connector_thickness,
                    "minimum_web": settings.minimum_web,
                    "tab_count": settings.tab_count,
                    "min_section_depth": settings.min_section_depth,
                    "force_flat_back": settings.force_flat_back,
                    "back_plane_offset": settings.back_plane_offset,
                    "slots": [
                        {
                            "part": slot.part_name,
                            "x": slot.center_x,
                            "z": slot.center_z,
                            "width": slot.width,
                            "length": slot.length,
                        }
                        for slot in panel.slots
                    ],
                },
                "wave_panels": {
                    "count": len(getattr(job.wave_panels, "panels", []) or []),
                    "amplitude": getattr(job.wave_panels, "amplitude", 0) if job.wave_panels else 0,
                    "wavelength": getattr(job.wave_panels, "wavelength", 0) if job.wave_panels else 0,
                    "phase_deg": getattr(job.wave_panels, "phase_deg", 0) if job.wave_panels else 0,
                    "placement": getattr(job.wave_panels, "placement", "") if job.wave_panels else "",
                },
                "warnings": job.warnings,
                "validation": None if validation is None else {
                    "ok": validation.ok,
                    "checks": validation.checks,
                    "errors": validation.errors,
                    "warnings": validation.warnings,
                },
            })
        finally:
            TASKS.pop(token, None)

    def export_job(self, job_id: str) -> None:
        job = JOBS.get(job_id)
        if job is None:
            self.send_json({"error": "خروجی پیدا نشد؛ ابتدا تولید نهایی را انجام دهید."}, 404)
            return
        try:
            folder = WORK / f"export_{job_id}"
            parts, panel, stack, wave = export_dxf(job, folder)
            archive = WORK / f"ELEVEN_DXF_{job_id}.zip"
            with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as output:
                output.write(parts, parts.name)
                output.write(panel, panel.name)
                output.write(stack, stack.name)
                if wave is not None and wave.is_file():
                    output.write(wave, wave.name)
            data = archive.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition", 'attachment; filename="ELEVEN_S1.1.10.0_DXF.zip"')
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except Exception as exc:
            self.send_json({"error": fa_error(exc)}, 400)


def main() -> None:
    # Independent port range so other ELEVEN tools keep running.
    requested_port = int(os.environ.get("ELEVEN_S1_PORT", "18231"))
    server = None
    for candidate in range(requested_port, requested_port + 10):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", candidate), Handler)
            break
        except OSError:
            continue
    if server is None:
        raise RuntimeError("No free ELEVEN S1.1 port was found between 18231 and 18240.")
    port = server.server_address[1]
    url = f"http://127.0.0.1:{port}/"
    print(f"ELEVEN Version S{__version__} running independently at {url}")
    if os.environ.get("ELEVEN_NO_BROWSER") != "1":
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
