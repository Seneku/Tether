"""Pictures: viewport and PIE screenshots, ue look contact sheets, PNG read/write/diff."""
import contextlib
import json
import os
import re
import sys
import time

from .core import (
    CliError, compact_error, compile_cpu, editor_mode, log_end, log_path, parse_res, read_json, stamped_name, state_dir,
    unwrap, write_json, zlib,
)
from .transport import py_json
from .calls import flush_editor_log


NO_ANNOTATIONS = {"gridSpacing": 0, "gridExtent": 0, "gridHeight": 0, "maxLabelDistance": 0,
                  "classFilter": {"refPath": ""}, "maxLabels": 0}
ANNOTATIONS = {"gridSpacing": 500, "gridExtent": 10000, "gridHeight": 0, "maxLabelDistance": 5000,
               "classFilter": {"refPath": ""}, "maxLabels": 15}


def pie_screenshot(ctx, args, pose=None):
    """The PIE world as the player sees it. Epic's CaptureViewport renders the *editor* world even during
    PIE (from whatever pose it is given), so take a HighResShot in the PIE world and collect the file.
    With --actor/--at/--look-at/--rot, a spectator shot: the player's view moves to a camera at that pose for
    the one shot, then comes back (the game keeps running)."""
    if args.annotate:
        raise CliError("--annotate works outside PIE, or in Simulate (ue pie start --simulate)")
    if pose and spectator(args):
        # The debug camera takes the console (HighResShot would go to it), so capture through the Slate inspector.
        if getattr(args, "_placed", False):  # a series (--frames/--burst) placed the camera once already
            return spectator_capture(ctx)
        with spectator_view(ctx, pose):
            return spectator_capture(ctx)
    if getattr(args, "hud", False):
        client = pie_client_target(ctx)
        if client:  # a multiplayer client plays in a window of its own: capture that window, not the host's
            return client_window_shot(ctx, client)
        return hud_screenshot(ctx)
    return highres_shot(ctx, getattr(args, "res", None) or "1", pie=True)


def spectator(args):
    return bool(args.actor or args.at or args.look_at or args.rot)


@contextlib.contextmanager
def spectator_view(ctx, pose):
    """The PIE player's view moved to `pose` (a debug camera) for what runs inside, then given back."""
    ctx.tether("pie_view", pose=json.dumps({"location": pose["location"], "rotation": pose["rotation"]}))
    try:
        time.sleep(0.2)  # a frame or two from the new viewpoint
        yield
    finally:
        ctx.tether("pie_view", pose="")


def spectator_capture(ctx):
    raw = widget_screenshot(ctx, {})
    if raw is None:
        raise CliError("a spectator shot in PIE needs the game viewport inside the editor window and Epic's Slate "
                       "inspector (ue init); or use Simulate (ue pie start --simulate) and ue shot --at")
    return raw


# --view: (the editor's ViewModeIndex member, the game's `viewmode` name)
VIEW_MODES = {
    "lit": ("LIT", "Lit"), "unlit": ("UNLIT", "Unlit"), "wireframe": ("WIREFRAME", "Wireframe"),
    "lightingonly": ("LIGHTING_ONLY", "LightingOnly"), "detaillighting": ("LIT_DETAIL_LIGHTING", "Lit_DetailLighting"),
    "shadercomplexity": ("SHADER_COMPLEXITY", "ShaderComplexity"), "quadoverdraw": ("QUAD_OVERDRAW", "QuadOverdraw"),
    "lightcomplexity": ("LIGHT_COMPLEXITY", "LightComplexity"), "lods": ("LOD_COLORATION", "LODColoration"),
    "nanite": ("VISUALIZE_NANITE", "VisualizeNanite"), "lumen": ("VISUALIZE_LUMEN", "VisualizeLumen"),
    "shadowcasters": ("SHADOW_CASTERS", "ShadowCasters"), "collision": ("COLLISION_VISIBILITY", "CollisionVis"),
    "lightmapdensity": ("LIGHTMAP_DENSITY", "LightmapDensity"),
    "vsm": ("VISUALIZE_VIRTUAL_SHADOW_MAP", "VisualizeVirtualShadowMap"),
}
VIEW_SET = """import json
A = unreal.AutomationLibrary
old = [A.get_editor_active_viewport_view_mode().name, A.get_editor_active_viewport_wireframe_opacity()]
A.set_editor_active_viewport_view_mode(unreal.ViewModeIndex.VMI_{mode})
if {wire}:
    A.set_editor_active_viewport_wireframe_opacity(1.0)  # the editor's default draws wireframes faintly
gi = unreal.SystemLibrary.get_console_variable_int_value('r.DynamicGlobalIlluminationMethod')
print(json.dumps(old + [gi]))
"""
VIEW_RESTORE = """A = unreal.AutomationLibrary
A.set_editor_active_viewport_view_mode(getattr(unreal.ViewModeIndex, {mode!r}))
A.set_editor_active_viewport_wireframe_opacity({opacity})
"""
VIEW_QUIET, VIEW_SETTLE_MAX = 1.5, 60  # seconds without shader-worker CPU / most to wait for a view's shaders


def settle_shaders(ctx, limit=VIEW_SETTLE_MAX):
    """Wait until the editor's shader workers have been idle for VIEW_QUIET seconds (a debug view mode's shaders
    compile on first use, and a capture before then misses what they draw). False when that cannot be measured."""
    pid = (ctx.beacon or {}).get("pid")
    last = compile_cpu(pid) if pid else None
    if last is None:
        return False
    started, quiet = time.time(), None
    while time.time() - started < limit:
        time.sleep(0.25)
        now = compile_cpu(pid)
        busy = max(0.0, now[1] - last[1]) > 0.02
        last, quiet = now, (None if busy else (quiet or time.time()))
        if quiet and time.time() - quiet >= VIEW_QUIET:
            return True
    return True


@contextlib.contextmanager
def view_mode(ctx, name, pie, warm):
    """The view `name` (VIEW_MODES) for the captures inside, then the previous one: the level viewport's view mode
    in the editor and Simulate (it is what CaptureViewport renders), the game viewport's `viewmode` in PIE. `warm`
    captures once so the mode's shaders start compiling, then this waits for them."""
    if not name:
        yield
        return
    editor_mode_name, game_mode_name = VIEW_MODES[name]
    if pie:
        ctx.tether("console", command=f"viewmode {game_mode_name}", pie=True)
        try:
            warm()
            settle_shaders(ctx)
            yield
        finally:
            with contextlib.suppress(CliError):
                ctx.tether("console", command="viewmode Lit", pie=True)
        return
    old = py_json(ctx, VIEW_SET.format(mode=editor_mode_name, wire=name == "wireframe"))
    if name == "lumen" and old[2] != 1:
        print("note: Lumen is not this project's global illumination (r.DynamicGlobalIlluminationMethod is "
              f"{old[2]}, not 1), so the Lumen view shows only a message", file=sys.stderr)
    try:
        warm()
        settle_shaders(ctx)
        yield
    finally:
        with contextlib.suppress(CliError):
            ctx.tether("py", code=VIEW_RESTORE.format(mode=old[0], opacity=old[1]))


SNAPSHOT_LINE = re.compile(r'^(\s*)(\w+)(?: "([^"]*)")?.*?\[pos=(-?\d+),(-?\d+) size=(\d+),(\d+)\]')


def slate_entries(snapshot):
    """[(indent, kind, title, x, y, w, h, ref)] for the lines of a Slate inspector snapshot."""
    rows = []
    for line in snapshot.splitlines():
        m = SNAPSHOT_LINE.match(line)
        ref = SNAPSHOT_REF.search(line)
        if m and ref:
            rows.append((len(m.group(1)), m.group(2), m.group(3) or "", int(m.group(4)), int(m.group(5)),
                         int(m.group(6)), int(m.group(7)), ref.group(1)))
    return rows


def find_window_or_tab(rows, title):
    """(window row, tab row or None, crop (x, y, w, h) in window units or None) for the window or dock tab whose title
    matches `title` (exactly, else as a case-insensitive substring)."""
    want = title.lower()
    candidates = [r for r in rows if r[1] in ("window", "tab") and r[2]]
    hit = next((r for r in candidates if r[2].lower() == want), None) or \
        next((r for r in candidates if want in r[2].lower()), None)
    if hit is None:
        names = list(dict.fromkeys(f'{r[1]} "{r[2]}"' for r in candidates))
        raise CliError(f'no editor window or tab "{title}" (open: {", ".join(names[:20]) or "none"})')
    i = rows.index(hit)
    window = next((r for r in reversed(rows[:i + 1]) if r[1] == "window"), None)
    if window is None:
        raise CliError(f'"{title}" is not inside an editor window')
    if hit is window:
        return window, None, None
    # A tab's content is not in the snapshot: it is the area below its header, as wide as the container its tab
    # well is in, down to the next tab well in that container (another row of tabs) or the container's bottom.
    parent = next((r for r in reversed(rows[:i]) if r[0] < hit[0] and r[5] and r[6]), window)
    x0, y0 = parent[3], hit[4]
    bottom = parent[4] + parent[6]
    for r in rows[i + 1:]:
        if r[0] < hit[0]:
            break
        if r[1] == "tab" and r[0] == hit[0] and r[4] > hit[4]:
            bottom = r[4]
            break
    rect = (x0 - window[3], y0 - window[4], parent[5], bottom - y0)  # x, y, width, height
    return window, hit, rect


def level_tab_hidden(rows, map_name):
    """The level editor's document tab (titled after the open map) when no viewport tab is in the snapshot: another
    document tab, such as the Message Log the editor opens after a PIE session that logged errors, is in front."""
    if any(r[1] == "tab" and r[2].startswith("Viewport") for r in rows):
        return None
    return next((r for r in rows if r[1] == "tab" and map_name and r[2] == map_name), None)


def front_level_viewport(ctx):
    """Bring the level editor's tab to the front when another document tab hides it: the PIE viewport is inside it,
    and `ue ui`, clicks, `expect-ui` and HUD or spectator shots find the game there. True when it had to."""
    try:
        text, failed = ctx.mcp.call("SlateInspectorToolset.SlateInspectorToolset", "Snapshot",
                                    {"ref": "", "maxDepth": 60, "bIncludeSourceLocations": False})
    except (CliError, AttributeError):
        return False
    value = unwrap(text) if not failed else ""
    rows = slate_entries(value if isinstance(value, str) else json.dumps(value))
    tab = level_tab_hidden(rows, ((ctx.beacon or {}).get("map") or "").rsplit("/", 1)[-1])
    if tab is None:
        return False
    ctx.mcp.call("SlateInspectorToolset.SlateInspectorToolset", "Click", {"ref": tab[7]})
    time.sleep(0.3)
    return True


def window_shot(ctx, title):
    """PNG bytes of an editor window or dock tab (`ue shot --window TITLE`), through the Slate inspector. A tab is
    brought to the front of its tab well first."""
    import base64

    def snapshot():
        text, failed = ctx.mcp.call("SlateInspectorToolset.SlateInspectorToolset", "Snapshot",
                                    {"ref": "", "maxDepth": 60, "bIncludeSourceLocations": False})
        if failed:
            raise CliError("the Slate inspector is not available (ue init enables Epic's SlateInspector toolset)")
        value = unwrap(text)
        return value if isinstance(value, str) else json.dumps(value)

    window, tab, rect = find_window_or_tab(slate_entries(snapshot()), title)
    if tab is not None:
        ctx.mcp.call("SlateInspectorToolset.SlateInspectorToolset", "Click", {"ref": tab[7]})
        time.sleep(0.3)
        window, tab, rect = find_window_or_tab(slate_entries(snapshot()), tab[2])
    text, failed = ctx.mcp.call("SlateInspectorToolset.SlateInspectorToolset", "Screenshot", {"ref": window[7]})
    value = unwrap(text) if not failed else None
    value = value.get("returnValue", value) if isinstance(value, dict) else None
    if not isinstance(value, dict) or not value.get("data"):
        raise CliError(f'the Slate inspector could not capture "{window[2]}"' + (f": {compact_error(text)}" if failed else ""))
    raw = base64.b64decode(value["data"])
    label = f'window "{window[2]}"' if tab is None else f'tab "{tab[2]}" of "{window[2]}"'
    if rect is not None:
        width = int.from_bytes(raw[16:20], "big") if raw[:4] == b"\x89PNG" else window[5]
        raw = png_crop(raw, rect, width / max(1, window[5]))
    return raw, label


def pie_client_target(ctx):
    """The client `ue pie target` picked (1, 2...), or 0 for the host / single-player."""
    status = str(ctx.tether("pie_net", action="status", clients=1, net="-", target=-1, game="-"))
    m = re.search(r"^\* (\d+):", status, re.M)
    return int(m.group(1)) if m else 0


def client_window_shot(ctx, client):
    """PNG of a multiplayer PIE client's own window (the game view and its UMG HUD), through the Slate inspector."""
    import base64
    text, failed = ctx.mcp.call("SlateInspectorToolset.SlateInspectorToolset", "Snapshot",
                                {"ref": "", "maxDepth": 3, "bIncludeSourceLocations": False})
    snapshot = unwrap(text) if not failed else ""
    snapshot = snapshot if isinstance(snapshot, str) else json.dumps(snapshot)
    ref = next((SNAPSHOT_REF.search(l).group(1) for l in snapshot.splitlines()
                if f"NetMode: Client {client}]" in l and SNAPSHOT_REF.search(l)), None)
    if ref is None:
        raise CliError(f"no PIE window for client {client} (is it a separate window? ue ui lists the windows)")
    text, failed = ctx.mcp.call("SlateInspectorToolset.SlateInspectorToolset", "Screenshot", {"ref": ref})
    value = unwrap(text) if not failed else None
    value = value.get("returnValue", value) if isinstance(value, dict) else None
    if not isinstance(value, dict) or not value.get("data"):
        raise CliError(f"the Slate inspector could not capture client {client}'s window")
    print(f"note: client {client}'s own window (ue pie target), with its UMG; canvas-drawn text is not in it")
    return base64.b64decode(value["data"])


def highres_shot(ctx, spec, pie, wait=15):
    """PNG bytes from HighResShot: "1" for the viewport's size, "2560x1440" for a real resolution (the scene is
    rendered at that size even though the offscreen viewport is small)."""
    start = log_end(ctx.project)
    ctx.tether("console", command=f"HighResShot {spec}", pie=pie)
    saved = re.compile(r"High resolution screenshot saved as (.+\.png)")
    deadline = time.time() + wait
    while time.time() < deadline:
        flush_editor_log(ctx)
        with open(log_path(ctx.project), "rb") as f:
            f.seek(start)
            match = saved.search(f.read().decode("utf-8", "replace"))
        if match and os.path.isfile(match.group(1).strip()):
            path = match.group(1).strip()
            with open(path, "rb") as f:
                raw = f.read()
            os.remove(path)  # it moves to Saved/Tether/shots (or -o) below
            return raw
        time.sleep(0.2)
    raise CliError(f"the screenshot did not appear within {wait} s (ue log --level Warning)", 4)


SNAPSHOT_BOX = re.compile(r"^(\s*)(\w+).*?\[pos=(-?\d+),(-?\d+) size=(\d+),(\d+)\]")
SNAPSHOT_REF = re.compile(r"\[ref=(\w+)\]")


def hud_rect(snapshot, view_w, view_h, dpi=1.0):
    """(x, y, w, h) of the game viewport inside the editor window, in screenshot pixels, from a Slate inspector
    snapshot: the smallest panel as wide as the viewport and at least as tall (the viewport tab's area), with
    the viewport at its bottom, below the tab's toolbar. None when no panel fits (PIE in its own window).
    The inspector prints absolute (desktop pixel) positions but DPI-independent sizes: positions are taken
    relative to the window, sizes times the DPI scale."""
    boxes = []
    for line in snapshot.splitlines():
        m = SNAPSHOT_BOX.match(line)
        if m:
            boxes.append(tuple(int(v) for v in m.groups()[2:]))
    if not boxes:
        return None
    wx, wy = boxes[0][:2]  # the window
    boxes = [(x - wx, y - wy, w * dpi, h * dpi) for x, y, w, h in boxes[1:]]
    # The viewport's panel: as wide as the viewport (give or take a border), taller by its tab and toolbar.
    slack = 4 * dpi + 1
    fits = [b for b in boxes if abs(b[2] - view_w) <= slack and view_h - slack <= b[3] < view_h + 600 * dpi]
    if not fits:
        return None
    x, y, w, h = min(fits, key=lambda b: b[3])
    return x + (w - view_w) / 2, y + h - view_h, view_w, view_h


def hud_widget(snapshot, view_w, view_h):
    """(ref, (w, h) in DPI-independent units, dpi) of the panel holding the game viewport, for a widget capture;
    the DPI scale is unknown here, so the usual ones are tried. None when no panel fits."""
    for dpi in (1.0, 1.25, 1.5, 1.75, 2.0, 2.5, 3.0):
        rect = hud_rect(snapshot, view_w, view_h, dpi)
        if rect is None:
            continue
        for line in snapshot.splitlines()[1:]:
            m, ref = SNAPSHOT_BOX.match(line), SNAPSHOT_REF.search(line)
            if m and ref and abs(int(m.group(5)) * dpi - (rect[2] + 2 * (rect[0] - int(m.group(3))))) < 1.5 \
                    and int(m.group(4)) + int(m.group(6)) * dpi - (rect[1] + rect[3]) < 1.5 \
                    and int(m.group(3)) <= rect[0] + 1 and int(m.group(4)) <= rect[1] + 1:
                return ref.group(1), (int(m.group(5)), int(m.group(6))), dpi
    return None


def widget_screenshot(ctx, cache):
    """The game viewport with its UMG HUD: Epic's Slate inspector renders the viewport's panel, cropped to the
    viewport. Canvas-drawn text (AHUD::DrawText, on-screen debug messages) is not in it; None when the panel
    cannot be found (PIE in its own window, no inspector), so callers fall back to hud_screenshot."""
    captured = widget_capture(ctx, cache)
    return png_crop(*captured) if captured else None


def widget_capture(ctx, cache):
    """(PNG of the viewport's panel, the viewport's rectangle in it) in a few hundredths of a second, or None. The
    crop is left to the caller: decoding a large PNG in Python takes far longer than the capture."""
    import base64
    if "widget" not in cache:
        size = str(ctx.tether("py", code="s = unreal.WidgetLayoutLibrary.get_viewport_size(unreal.get_editor_subsystem("
                                         "unreal.UnrealEditorSubsystem).get_game_world())\nf'{s.x:.0f} {s.y:.0f}'"))
        view_w, view_h = (int(v) for v in size.strip().strip("'").split())
        text, failed = ctx.mcp.call("SlateInspectorToolset.SlateInspectorToolset", "Snapshot",
                                    {"ref": "", "maxDepth": 60, "bIncludeSourceLocations": False})
        snapshot = unwrap(text) if not failed else ""
        found = hud_widget(snapshot if isinstance(snapshot, str) else "", view_w, view_h)
        cache["widget"] = (found, view_w, view_h)
    found, view_w, view_h = cache["widget"]
    if not found:
        return None
    ref, (box_w, box_h), dpi = found
    text, failed = ctx.mcp.call("SlateInspectorToolset.SlateInspectorToolset", "Screenshot", {"ref": ref})
    value = unwrap(text) if not failed else None
    value = value.get("returnValue", value) if isinstance(value, dict) else None
    if not isinstance(value, dict) or not value.get("data"):
        return None
    raw = base64.b64decode(value["data"])
    image_w = int.from_bytes(raw[16:20], "big")
    image_h = int.from_bytes(raw[20:24], "big")
    scale = image_w / (box_w * dpi)  # screenshot pixels per screen pixel
    w, h = view_w * scale, view_h * scale
    return raw, ((image_w - w) / 2, image_h - h, w, h)


def crop_pixels(image, rect, scale=1.0):
    """(width, height, 3, RGB bytes) of a rectangle (times scale for pixels) of a decoded image (png_read)."""
    width, height, channels, pixels = image
    x0, y0 = max(0, round(rect[0] * scale)), max(0, round(rect[1] * scale))
    x1, y1 = min(width, round((rect[0] + rect[2]) * scale)), min(height, round((rect[1] + rect[3]) * scale))
    rows = []
    for y in range(y0, y1):
        line = pixels[(y * width + x0) * channels:(y * width + x1) * channels]
        if channels == 3:
            rows.append(bytes(line))
        else:  # drop alpha (or widen grey) with slices, not a loop per byte
            rgb = bytearray((x1 - x0) * 3)
            for c in range(3):
                rgb[c::3] = line[min(c, channels - 1)::channels] if channels >= 3 else line[0::channels]
            rows.append(bytes(rgb))
    return x1 - x0, y1 - y0, 3, b"".join(rows)


def png_crop(raw, rect, scale=1.0):
    """PNG bytes of a rectangle (window units, times scale for pixels) of a PNG."""
    width, height, _, rgb = crop_pixels(png_read(raw), rect, scale)
    return png_bytes(width, height, rgb)


def hud_screenshot(ctx, cache=None):
    """The PIE viewport as the player sees it, UMG HUD included. HighResShot renders the scene only; `shot showui`
    grabs the whole editor window (UI and all), so take that and crop it to the game viewport, located through
    Epic's Slate inspector."""
    size = str(ctx.tether("py", code="s = unreal.WidgetLayoutLibrary.get_viewport_size(unreal.get_editor_subsystem("
                                     "unreal.UnrealEditorSubsystem).get_game_world())\nf'{s.x:.0f} {s.y:.0f}'"))
    view_w, view_h = (int(v) for v in size.strip().strip("'").split())
    start = log_end(ctx.project)
    ctx.tether("console", command="shot showui", pie=True)
    taken = re.compile(r'Screenshot "([^"]+)" taken with size: (\d+) x (\d+)')
    folder = os.path.join(os.path.dirname(ctx.project), "Saved", "Screenshots")
    deadline, path = time.time() + 15, None
    while time.time() < deadline and not path:
        flush_editor_log(ctx)
        with open(log_path(ctx.project), "rb") as f:
            f.seek(start)
            match = taken.search(f.read().decode("utf-8", "replace"))
        if match:
            names = [os.path.join(d, n) for d, _, files in os.walk(folder) for n in files if n == f"{match.group(1)}.png"]
            path = max(names, key=os.path.getmtime) if names else None
        time.sleep(0.2)
    if not path:
        raise CliError("the window screenshot did not appear within 15 s (ue log --level Warning)", 4)
    time.sleep(0.3)  # the file is written after the log line
    with open(path, "rb") as f:
        raw = f.read()
    os.remove(path)  # it moves to Saved/Tether/shots (or -o)
    if cache and cache.get("rect"):  # a film: the viewport stays put, so locate it once
        return png_crop(raw, cache["rect"])
    text, is_error = ctx.mcp.call("SlateInspectorToolset.SlateInspectorToolset", "Snapshot",
                                  {"ref": "", "maxDepth": 60, "bIncludeSourceLocations": False})
    snapshot = unwrap(text) if not is_error else ""
    snapshot = snapshot if isinstance(snapshot, str) else json.dumps(snapshot)
    window = SNAPSHOT_BOX.match(snapshot.splitlines()[0]) if snapshot else None
    image_w = int.from_bytes(raw[16:20], "big")
    window_w = int(window.group(5)) if window and int(window.group(5)) else image_w
    dpi = image_w / window_w  # the window's size is in DPI-independent units, the screenshot in pixels
    rect = hud_rect(snapshot, view_w, view_h, dpi) if snapshot else None
    if rect is None:
        print(f"note: could not place the {view_w}x{view_h} game viewport in the window (DPI scale {dpi:g}; Slate "
              "inspector unavailable, or PIE runs in its own window): this is the whole window")
        return raw
    cropped = png_crop(raw, rect)
    if cache is None:  # a single shot: check the crop is the game view (a film checks nothing per frame)
        widget = widget_screenshot(ctx, {})
        if widget is not None and thumb_difference(cropped, widget) > HUD_MISMATCH:
            # The window grab did not line up with the viewport (another editor layout, or the offscreen view not
            # composited into the window): the Slate inspector's capture of the viewport itself is right.
            print("note: the window screenshot did not line up with the game view, so this is the viewport's own "
                  "capture: UMG is in it; canvas-drawn text (AHUD, debug messages) usually is too, but check the "
                  "image if you rely on it")
            return widget
    if cache is not None:
        cache.update(rect=rect)
    return cropped


HUD_MISMATCH = 10  # mean grey-level difference (0-255) of 32x20 thumbnails above which a crop is not the game view


def thumb_difference(png_a, png_b, size=(32, 20)):
    """Mean absolute grey difference between two PNGs shrunk to the same small size (overlaid text barely moves it,
    a different picture does)."""
    def thumb(raw):
        w, h, channels, pixels = png_read(raw)
        out = []
        for ty in range(size[1]):
            y = min(h - 1, int((ty + 0.5) * h / size[1]))
            for tx in range(size[0]):
                i = (y * w + min(w - 1, int((tx + 0.5) * w / size[0]))) * channels
                out.append(sum(pixels[i:i + min(3, channels)]) / min(3, channels))
        return out
    a, b = thumb(png_a), thumb(png_b)
    return sum(abs(x - y) for x, y in zip(a, b)) / len(a)


def capture_viewport(ctx, pose, annotations=None, ui=False):
    """PNG bytes of the level viewport rendered from `pose` (Epic's CaptureViewport)."""
    text, is_error = ctx.mcp.call("EditorToolset.EditorAppToolset", "CaptureViewport",
                                  {"captureTransform": pose, "annotations": annotations or NO_ANNOTATIONS, "bShowUI": ui})
    if is_error:
        hint = " (the viewport is not rendering: minimised window or -nullrhi?)" if "zero size" in text else ""
        raise CliError(compact_error(text) + hint)
    value = unwrap(text)
    image = value.get("image") if isinstance(value, dict) else None
    if not image or not image.get("data"):
        raise CliError("CaptureViewport returned no image")
    import base64
    return base64.b64decode(image["data"])


# 3x5 pixel glyphs (drawn 2x): numbers and times on pictures, stdlib only.
GLYPHS = {"0": "111101101101111", "1": "010110010010111", "2": "111001111100111", "3": "111001111001111",
          "4": "101101111001001", "5": "111100111001111", "6": "111100111101111", "7": "111001010010010",
          "8": "111101111101111", "9": "111101111001111", ".": "000000000000010", "-": "000000111000000",
          "s": "000011010001110", "+": "000010111010000"}


def draw_text(rgb, width, x, y, text, colour=(255, 255, 255), shadow=(0, 0, 0)):
    """Draw digits (and . - s) into an RGB bytearray `width` pixels wide, 2x scale, with a dark shadow."""
    height = len(rgb) // (width * 3)
    for dx, dy, col in ((1, 1, shadow), (0, 0, colour)):
        for k, ch in enumerate(text):
            for i, bit in enumerate(GLYPHS.get(ch, "")):
                if bit != "1":
                    continue
                for sy in range(2):
                    for sx in range(2):
                        px, py = x + dx + k * 8 + (i % 3) * 2 + sx, y + dy + (i // 3) * 2 + sy
                        if 0 <= px < width and 0 <= py < height:
                            o = (py * width + px) * 3
                            rgb[o:o + 3] = bytes(col)


def contact_sheet(tiles, columns=3, tile_width=400, gap=4):
    """Tile decoded images (width, height, channels, bytes) into one RGB image, each scaled to tile_width
    (nearest neighbour). Returns (width, height, rgb bytes)."""
    scaled = []
    for w, h, c, px in tiles:
        tw, th = tile_width, max(1, round(h * tile_width / w))
        out = bytearray(tw * th * 3)
        for y in range(th):
            sy = min(h - 1, y * h // th) * w
            for x in range(tw):
                si = (sy + min(w - 1, x * w // tw)) * c
                o = (y * tw + x) * 3
                out[o:o + 3] = px[si:si + 3] if c >= 3 else bytes([px[si]] * 3)
        scaled.append((tw, th, out))
    rows = (len(scaled) + columns - 1) // columns
    th = max(t[1] for t in scaled)
    width, height = columns * tile_width + (columns + 1) * gap, rows * th + (rows + 1) * gap
    sheet = bytearray([24, 24, 24] * (width * height))
    for i, (tw, tht, px) in enumerate(scaled):
        x0, y0 = gap + (i % columns) * (tile_width + gap), gap + (i // columns) * (th + gap)
        for y in range(tht):
            start = ((y0 + y) * width + x0) * 3
            sheet[start:start + tw * 3] = px[y * tw * 3:(y + 1) * tw * 3]
    return width, height, bytes(sheet)


# Camera yaw/pitch per view: an asset faces +X, so "front" looks back along -X from +X.
LOOK_VIEWS = (("front", 180, -10), ("right", 270, -10), ("back", 0, -10), ("left", 90, -10), ("top", 180, -89),
              ("3/4", 225, -25))


# `ue look --studio`: light the staged asset like the material editor's preview, whatever the level's lighting.
# Transient actors (labelled TetherStudio_*, the level stays clean), removed after the shots.
STUDIO = """sub = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
c = unreal.Vector({cx}, {cy}, {cz})
sky = sub.spawn_actor_from_class(unreal.SkyLight, c, unreal.Rotator(), transient=True)
sky.set_actor_label('TetherStudio_Sky')
comp = sky.get_component_by_class(unreal.SkyLightComponent)
comp.set_mobility(unreal.ComponentMobility.MOVABLE)
comp.set_editor_property('source_type', unreal.SkyLightSourceType.SLS_SPECIFIED_CUBEMAP)
comp.set_editor_property('cubemap', unreal.load_asset('/Engine/EditorMaterials/AssetViewer/EpicQuadPanorama_CC+EV1'))
comp.set_editor_property('intensity', 1.0)
comp.recapture_sky()
key = sub.spawn_actor_from_class(unreal.DirectionalLight, c, unreal.Rotator(roll=0, pitch=-40, yaw=135), transient=True)
key.set_actor_label('TetherStudio_Key')
light = key.get_component_by_class(unreal.DirectionalLightComponent)
light.set_mobility(unreal.ComponentMobility.MOVABLE)
light.set_editor_property('intensity', 4.0)
light.set_editor_property('forward_shading_priority', 10)
pp = sub.spawn_actor_from_class(unreal.PostProcessVolume, c - unreal.Vector(0, 0, 1e6), unreal.Rotator(),
                                transient=True)  # unbound, so anywhere: far below, where its box draws in no shot
pp.set_actor_label('TetherStudio_Exposure')
pp.set_editor_property('unbound', True)
pp.set_editor_property('priority', 100000.0)
s = pp.get_editor_property('settings')
for name, value in (('auto_exposure_method', unreal.AutoExposureMethod.AEM_HISTOGRAM),
                    ('auto_exposure_min_brightness', -10.0), ('auto_exposure_max_brightness', 20.0),
                    ('auto_exposure_bias', 0.0)):
    s.set_editor_property('override_' + name, True)
    s.set_editor_property(name, value)
pp.set_editor_property('settings', s)
"""
STUDIO_REMOVE = ("sub = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)\n"  # transient: not in level lists
                 "w = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_editor_world()\n"
                 "for a in unreal.GameplayStatics.get_all_actors_of_class(w, unreal.Actor):\n"
                 "    if a.get_actor_label().startswith('TetherStudio_'):\n"
                 "        sub.destroy_actor(a)\n")


def cmd_look(ctx, args):
    """Render an asset from six angles into one contact sheet (front, right, back / left, top, three-quarter)."""
    import math
    launch = editor_mode(ctx.project, ctx.beacon)
    if launch.get("headless") and not launch.get("render"):
        raise CliError("this editor runs headless with -nullrhi and cannot render; reopen it with "
                       "`ue open --headless --render`")
    if (ctx.beacon or {}).get("pie"):
        raise CliError("stop PIE first (ue pie stop): the asset is staged in the editor level")
    stage = ctx.tether("look_stage", asset=args.asset, action="place")
    stage = json.loads(stage) if isinstance(stage, str) else stage
    tiles = []
    try:
        cx, cy, cz = stage["center"]
        if getattr(args, "studio", False):
            ctx.tether("py", code=STUDIO.format(cx=cx, cy=cy, cz=cz))
            time.sleep(1.0)  # the sky light capture and the exposure settle over a few frames
        distance = stage["radius"] * args.distance
        for name, yaw, pitch in LOOK_VIEWS:
            yr, pr = math.radians(yaw), math.radians(pitch)
            forward = (math.cos(pr) * math.cos(yr), math.cos(pr) * math.sin(yr), math.sin(pr))
            pose = {"location": {"x": cx - forward[0] * distance, "y": cy - forward[1] * distance,
                                 "z": cz - forward[2] * distance},
                    "rotation": {"pitch": pitch, "yaw": yaw, "roll": 0}, "scale": {"x": 1, "y": 1, "z": 1}}
            tiles.append(png_read(capture_viewport(ctx, pose)))
    finally:
        with __import__("contextlib").suppress(CliError):
            ctx.tether("look_stage", asset=args.asset, action="remove")
        if getattr(args, "studio", False):
            with __import__("contextlib").suppress(CliError):
                ctx.tether("py", code=STUDIO_REMOVE)
    width, height, rgb = contact_sheet(tiles, columns=3, tile_width=args.tile)
    if args.output:
        path = os.path.abspath(args.output)
    else:
        folder = os.path.join(state_dir(ctx.project), "shots")
        os.makedirs(folder, exist_ok=True)
        path = stamped_name(folder, f"look-{args.asset.rstrip('/').rsplit('/', 1)[-1].split('.')[0]}", ".png")
    png_write(path, width, height, rgb)
    print(f"{path}  {width}x{height}")
    print(f"{args.asset} ({stage.get('class')}, about {stage['radius'] * 2 / 100:.1f} m across): "
          "top row front, right, back; bottom row left, top, three-quarter")
    return 0


GAME_TIME = ("w = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_game_world()\n"
             "{set}print(unreal.GameplayStatics.get_time_seconds(w), unreal.GameplayStatics.get_global_time_dilation(w))")
SET_DILATION = "unreal.GameplayStatics.set_global_time_dilation(w, {})\n"
SHOT_SECONDS = 1.5  # what one capture costs in real time, with margin: HighResShot, then the file in the log


def game_clock(ctx, dilation=None):
    """(game seconds, time dilation) of the PIE world, setting the dilation first when given."""
    out = ctx.tether("py", code=GAME_TIME.format(set=SET_DILATION.format(dilation) if dilation else ""))
    now, dil = str(out).strip().splitlines()[-1].split()
    return float(now), float(dil)


def shot_frames(ctx, args, pie, capture, burst=False):
    """`ue shot --frames N --interval S [--after CMD]`: N captures S seconds apart in one contact sheet. With a game
    world (PIE or Simulate) the game is slowed while capturing, so intervals shorter than a capture still land,
    and each tile is labelled with its game time; in the editor the interval is real time. `--burst N --every S`
    is the same series kept as N full-size PNGs in one folder."""
    n, interval = (args.burst, args.every) if burst else (args.frames, args.interval)
    if not 2 <= n <= (200 if burst else 36):
        raise CliError("--burst takes 2 to 200" if burst else "--frames takes 2 to 36")
    if interval <= 0:
        raise CliError("--interval is in seconds and above 0")
    tiles, stamps = [], []
    if pie:
        _, before = game_clock(ctx)
        dilation = max(0.01, min(1.0, interval / SHOT_SECONDS))
        game_clock(ctx, dilation)
        try:
            if args.after:
                ctx.tether("console", command=args.after, pie=True)
            start = None
            for i in range(n):
                now, _ = game_clock(ctx)
                start = now if start is None else start
                wait = (start + i * interval - now) / dilation
                if wait > 0:
                    time.sleep(wait)
                    now, _ = game_clock(ctx)
                stamps.append(now - start)
                tiles.append(capture() if burst else png_read(capture()))
        finally:
            with contextlib.suppress(CliError):
                game_clock(ctx, before)
        note = (f"game slowed to {dilation:.2g}x while capturing (time dilation {before:g} put back)" if dilation < 1
                else "game at its own speed")
    else:
        if args.after:
            ctx.tether("console", command=args.after, pie=False)
        start = time.time()
        for i in range(n):
            wait = start + i * interval - time.time()
            if wait > 0:
                time.sleep(wait)
            stamps.append(time.time() - start)
            tiles.append(capture() if burst else png_read(capture()))
        late = [s - i * interval for i, s in enumerate(stamps) if s - i * interval > interval / 2]
        note = "real time (no game world to slow: PIE or Simulate for game time)" + (
            f"; {len(late)} capture(s) came late, a capture takes longer than --interval" if late else "")
    if burst:
        folder = os.path.abspath(args.output) if args.output else \
            stamped_name(os.path.join(state_dir(ctx.project), "shots"), "burst", "")
        os.makedirs(folder, exist_ok=True)
        paths = []
        for i, raw in enumerate(tiles):
            paths.append(os.path.join(folder, f"{i + 1:03d}.png"))
            with open(paths[-1], "wb") as f:
                f.write(raw)
        size = f"{int.from_bytes(tiles[0][16:20], 'big')}x{int.from_bytes(tiles[0][20:24], 'big')}"
        print(f"{folder}  {n} PNGs ({size}), 001.png .. {n:03d}.png")
        print(f"at +" + ", +".join(f"{s:.2f}" for s in stamps) + f" s; {note}")
        ctx.data = {"folder": folder, "paths": paths, "times": stamps}
        return 0
    columns = n if n <= 3 else 2 if n == 4 else 3 if n <= 9 else 4
    tile_width = 480 if n <= 6 else 360
    width, height, rgb = contact_sheet(tiles, columns=columns, tile_width=tile_width)
    rgb, gap = bytearray(rgb), 4
    w0, h0 = tiles[0][0], tiles[0][1]
    th = max(1, round(h0 * tile_width / w0))
    for i, stamp in enumerate(stamps):
        x0, y0 = gap + (i % columns) * (tile_width + gap), gap + (i // columns) * (th + gap)
        draw_text(rgb, width, x0 + 6, y0 + 6, f"+{stamp:.2f}s")
    if args.output:
        path = os.path.abspath(args.output)
    else:
        folder = os.path.join(state_dir(ctx.project), "shots")
        os.makedirs(folder, exist_ok=True)
        path = stamped_name(folder, "frames", ".png")
    png_write(path, width, height, bytes(rgb))
    print(f"{path}  {width}x{height}  {n} frames, {columns} per row")
    print(f"at +" + ", +".join(f"{s:.2f}" for s in stamps) + f" s; {note}")
    ctx.data = {"path": path, "times": stamps}
    return 0


CLEAR_VIEW = """import json, re
s = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem)
w = (s.get_game_world() if {pie} else None) or s.get_editor_world()
cam = unreal.Vector({cx}, {cy}, {cz})
spec, ignore = {target!r}, []
nums = re.findall(r'-?[\\d.]+', spec)
if len(nums) == 3 and re.fullmatch(r'[-\\d.,\\s]+', spec):
    target = unreal.Vector(*map(float, nums))
else:
    hit = [a for a in unreal.GameplayStatics.get_all_actors_of_class(w, unreal.Actor)
           if a.get_actor_label() == spec or a.get_name() == spec]
    if not hit:
        raise RuntimeError('--clear: no actor ' + spec)
    target, ignore = hit[0].get_actor_bounds(False)[0], [hit[0]]
r = unreal.SystemLibrary.line_trace_single(w, target, cam, unreal.TraceTypeQuery.TRACE_TYPE_QUERY1, True, ignore,
                                           unreal.DrawDebugTrace.NONE, True)
t = r.to_dict() if r is not None else {{}}  # HitResult's fields are protected; to_dict reads them
out = None
if t.get('blocking_hit'):
    p, d = t['location'], (target - cam)
    d = d / max(d.length(), 1.0)
    p = p + d * 60.0
    out = [p.x, p.y, p.z, (cam - p).length(), (t['hit_actor'].get_actor_label() if t.get('hit_actor') else '')]
print(json.dumps(out))
"""


def clear_view(ctx, pose, target, pie):
    """`ue shot --clear`: when something stands between the camera and what it looks at (the camera ended up
    inside a building), move the camera along the view line to just in front of it."""
    if not target:
        raise CliError("--clear needs --look-at or --actor: the point the camera must see")
    loc = pose["location"]
    moved = py_json(ctx, CLEAR_VIEW.format(pie=bool(pie), cx=loc["x"], cy=loc["y"], cz=loc["z"], target=target))
    if moved:
        pose["location"] = {"x": moved[0], "y": moved[1], "z": moved[2]}
        print(f"note: --clear moved the camera {moved[3] / 100:.1f} m along the view line, in front of "
              f"{moved[4] or 'what blocked it'}")


def shot_args(**given):
    """Arguments for calling cmd_shot from other commands: every option at its default, then `given`. A unit
    test keeps these in step with the `shot` parser, so a new option cannot break the callers again."""
    defaults = dict(actor=None, at=None, look_at=None, rot=None, annotate=False, grid_z=0, ui=False, hud=False,
                    output=None, baseline=None, compare=None, tolerance=0.5, threshold=32, res=None, frames=None,
                    interval=0.25, after=None, clear=False, view=None, burst=None, every=0.5, window=None)
    import argparse
    return argparse.Namespace(**dict(defaults, **given))


def baseline_pose(ctx, name):
    """(at, rot) text of a `ue shot --baseline NAME` camera."""
    baselines = os.path.join(state_dir(ctx.project), "shots", "baselines")
    meta = read_json(os.path.join(baselines, f"{name}.json"))
    if not meta:
        names = sorted(f[:-5] for f in os.listdir(baselines) if f.endswith(".json")) if os.path.isdir(baselines) else []
        raise CliError(f"no shot baseline '{name}'" + (f" (have: {', '.join(names)})" if names else
                                                       " (save one with ue shot ... --baseline NAME)"))
    loc, rot = meta["pose"]["location"], meta["pose"]["rotation"]
    return f"{loc['x']},{loc['y']},{loc['z']}", f"{rot['pitch']},{rot['yaw']},{rot['roll']}"


@contextlib.contextmanager
def camera_at(ctx, args):
    """The view `ue gpu` / `ue perf` measure, put where --at/--look-at/--rot/--actor or --camera (a shot baseline's
    camera) say for the measurement, then back: the player's view in PIE (a spectator camera), else the level
    viewport's camera. Nothing to do without those options."""
    at, look_at, rot, actor = (getattr(args, k, None) for k in ("at", "look_at", "rot", "actor"))
    if getattr(args, "camera", None):
        at, rot = baseline_pose(ctx, args.camera)
        look_at = actor = None
    if not (at or look_at or rot or actor):
        yield None
        return
    pose = ctx.tether("shot_pose", actor=actor or "", at=at or "", look_at=look_at or "", rot=rot or "")
    pie, simulate = pose.pop("pie", False), pose.pop("simulate", False)
    loc, r = pose["location"], pose["rotation"]
    if pie and not simulate:
        ctx.tether("pie_view", pose=json.dumps({"location": loc, "rotation": r}))
        try:
            time.sleep(0.3)  # a few frames from there before measuring
            yield pose
        finally:
            ctx.tether("pie_view", pose="")
        return
    camera = ctx.tether("py", code="c = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem)."
                                   "get_level_viewport_camera_info()\nimport json\n"
                                   "v = next(x for x in c if isinstance(x, unreal.Vector))\n"
                                   "r = next(x for x in c if isinstance(x, unreal.Rotator))\n"
                                   "json.dumps([[v.x, v.y, v.z], [r.pitch, r.yaw, r.roll]])")
    camera = json.loads(camera) if isinstance(camera, str) else camera
    place = ("unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).set_level_viewport_camera_info("
             "unreal.Vector({0}, {1}, {2}), unreal.Rotator(pitch={3}, yaw={4}, roll={5}))")
    ctx.tether("py", code=place.format(loc["x"], loc["y"], loc["z"], r["pitch"], r["yaw"], r["roll"]))
    try:
        time.sleep(0.3)
        yield pose
    finally:
        ctx.tether("py", code=place.format(*camera[0], *camera[1]))


MESH_BUILDING = re.compile(r"LogStaticMesh: (?:Display: )?Building static mesh (\S+)")
MESH_BUILT = re.compile(r"LogStaticMesh: (?:Display: )?Built static mesh \[[\d.]+s\] (\S+)")
MESH_LOG_TAIL = 4 * 1024 * 1024  # bytes of the log searched for mesh builds when no shot was taken before


def mesh_builds(lines):
    """(names still building, names built) from the editor log's static mesh build lines."""
    pending, built = {}, []
    for line in lines:
        m = MESH_BUILDING.search(line)
        if m:
            pending[m.group(1)] = pending.get(m.group(1), 0) + 1
            continue
        m = MESH_BUILT.search(line)
        if m:
            name = m.group(1).rsplit(".", 1)[-1].rsplit("/", 1)[-1]
            built.append(name)
            if pending.get(name):
                pending[name] -= 1
    return [n for n, count in pending.items() if count > 0], built


def settle_mesh_builds(ctx, warm, wait=180):
    """Before a shot: wait for static meshes still building, and when meshes were rebuilt since the last shot, call
    warm() (render a few frames at the shot's view) so Nanite streaming, render state and lighting catch up. An
    offscreen editor renders only when asked, so after 358 road tiles were rebuilt in place, shots showed missing
    decks and wrong materials for minutes. Returns the meshes rebuilt since the last shot."""
    path = os.path.join(state_dir(ctx.project), "last-shot.json")
    log = log_path(ctx.project)
    if not os.path.isfile(log):
        return []
    state = read_json(path) or {}
    size = os.path.getsize(log)
    start = state["offset"] if state.get("log") == log and 0 <= state.get("offset", -1) <= size else max(0, size - MESH_LOG_TAIL)

    def read():
        with open(log, "rb") as f:
            f.seek(start)
            return f.read().decode("utf-8", "replace").splitlines()
    pending, built = mesh_builds(read())
    if pending:
        started = time.time()
        while pending and time.time() - started < wait:
            time.sleep(0.5)
            flush_editor_log(ctx)
            pending, built = mesh_builds(read())
        print(f"note: waited {time.time() - started:.0f}s for static meshes still building"
              + (f"; {len(pending)} still are ({', '.join(pending[:3])})" if pending else ""), file=sys.stderr)
    write_json(path, {"log": log, "offset": os.path.getsize(log)})
    if built:
        names = list(dict.fromkeys(built))
        warm()
        print(f"note: {len(names)} static mesh(es) were rebuilt since the last shot ({', '.join(names[:3])}"
              + (", ..." if len(names) > 3 else "") + "): rendered a few frames at this view first, so streaming, "
              "render state and lighting catch up", file=sys.stderr)
    return built


def cmd_shot(ctx, args):
    """Capture the viewport (or the player's view during PIE) to a PNG and print its path."""
    launch = editor_mode(ctx.project, ctx.beacon)
    if launch.get("headless") and not launch.get("render"):
        raise CliError("this editor runs headless with -nullrhi and cannot render; "
                       "reopen it with `ue open --headless --render` (or windowed)")
    if getattr(args, "window", None):
        if any(getattr(args, k, None) for k in ("actor", "at", "look_at", "rot", "frames", "burst", "res", "view",
                                                "baseline", "compare", "hud")):
            raise CliError("--window captures an editor window or tab as it is: no camera, view or series options")
        raw, label = window_shot(ctx, args.window)
        return save_shot(ctx, args, raw, label)
    baselines = os.path.join(state_dir(ctx.project), "shots", "baselines")
    if args.compare:  # re-shoot the baseline's exact camera
        meta = read_json(os.path.join(baselines, f"{args.compare}.json"))
        if not meta:
            names = sorted(f[:-5] for f in os.listdir(baselines) if f.endswith(".json")) if os.path.isdir(baselines) else []
            raise CliError(f"no baseline '{args.compare}'" + (f" (have: {', '.join(names)})" if names else
                                                             " (save one with ue shot ... --baseline NAME)"))
        loc, rot = meta["pose"]["location"], meta["pose"]["rotation"]
        args.actor, args.look_at = None, None
        args.at, args.rot = f"{loc['x']},{loc['y']},{loc['z']}", f"{rot['pitch']},{rot['yaw']},{rot['roll']}"
    pose = ctx.tether("shot_pose", actor=args.actor or "", at=args.at or "", look_at=args.look_at or "",
                      rot=args.rot or "")
    pie, simulate = pose.pop("pie", False), pose.pop("simulate", False)
    if getattr(args, "clear", False):
        clear_view(ctx, pose, args.look_at or args.actor, pie)
    if getattr(args, "hud", False) and not (pie and not simulate):
        raise CliError("--hud needs PIE (ue pie start): it shows the game's UMG HUD over the player's view")
    if getattr(args, "res", None):
        parse_res(args.res)
        if getattr(args, "hud", False) or args.annotate or args.ui:
            raise CliError("--res renders the scene alone (HighResShot): not with --hud, --annotate or --ui")
    view = getattr(args, "view", None)
    if view and (getattr(args, "hud", False) or args.annotate):
        raise CliError("--view shows the scene in a debug view: not with --hud or --annotate")
    playing = pie and not simulate
    # a running game renders all the time; the editor's viewport only when asked: throwaway captures at this view
    settle_mesh_builds(ctx, (lambda: time.sleep(1.5)) if playing else
                       (lambda: [capture_viewport(ctx, pose, NO_ANNOTATIONS, False) for _ in range(6)]))
    if getattr(args, "frames", None) or getattr(args, "burst", None):
        series = "--burst" if getattr(args, "burst", None) else "--frames"
        if getattr(args, "frames", None) and getattr(args, "burst", None):
            raise CliError("--frames (one contact sheet) or --burst (separate PNGs), not both")
        if getattr(args, "res", None) or args.baseline or args.compare:
            raise CliError(f"{series} makes a series: not with --res, --baseline or --compare")
        if playing:
            capture = lambda: pie_screenshot(ctx, args, pose)
        else:
            annotations = dict(ANNOTATIONS, gridHeight=args.grid_z) if args.annotate else NO_ANNOTATIONS
            capture = lambda: capture_viewport(ctx, pose, annotations, args.ui)
        with contextlib.ExitStack() as stack:
            if playing and spectator(args):  # the debug camera once for the whole series (about 1.75 s a shot otherwise)
                stack.enter_context(spectator_view(ctx, pose))
                args._placed = True
            stack.enter_context(view_mode(ctx, view, playing, capture))
            return shot_frames(ctx, args, pie, capture, burst=series == "--burst")
    with view_mode(ctx, view, playing, lambda: shoot(ctx, args, pose, pie, simulate, launch)):
        raw = shoot(ctx, args, pose, pie, simulate, launch)
    return save_shot(ctx, args, raw, "", pose=pose, pie=pie, simulate=simulate)


def shoot(ctx, args, pose, pie, simulate, launch):
    """PNG bytes of one `ue shot` capture: the player's view in PIE, a HighResShot for --res, else CaptureViewport."""
    if pie and not simulate:  # Play: CaptureViewport would render the editor world, so HighResShot the game
        if getattr(args, "res", None) and (args.actor or args.at or args.look_at or args.rot):
            raise CliError("--res with a spectator pose in PIE is not supported; take the player's view, or Simulate")
        raw = pie_screenshot(ctx, args, pose)
    elif getattr(args, "res", None):  # the level viewport at a real resolution: move its camera, HighResShot, put back
        camera = ctx.tether("py", code="c = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem)."
                                       "get_level_viewport_camera_info()\nimport json\n"
                                       "v = next(x for x in c if isinstance(x, unreal.Vector))\n"
                                       "r = next(x for x in c if isinstance(x, unreal.Rotator))\n"
                                       "json.dumps([[v.x, v.y, v.z], [r.pitch, r.yaw, r.roll]])")
        camera = json.loads(camera) if isinstance(camera, str) else camera
        place = ("unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).set_level_viewport_camera_info("
                 "unreal.Vector({0}, {1}, {2}), unreal.Rotator(pitch={3}, yaw={4}, roll={5}))")
        loc, rot = pose["location"], pose["rotation"]
        ctx.tether("py", code=place.format(loc["x"], loc["y"], loc["z"], rot["pitch"], rot["yaw"], rot["roll"]))
        try:
            time.sleep(0.2)
            try:
                raw = highres_shot(ctx, args.res, pie=False, wait=8)
            except CliError as exc:
                if exc.code != 4 or launch.get("headless"):
                    raise
                # a windowed editor whose level viewport has not drawn since a map load (or is behind another
                # tab) takes no HighResShot until it redraws: invalidate it and try once more
                ctx.tether("py", code="unreal.EditorLevelLibrary.editor_invalidate_viewports()")
                time.sleep(0.5)
                raw = highres_shot(ctx, args.res, pie=False)
        except CliError as exc:
            if exc.code != 4:
                raise
            if launch.get("headless"):
                raise CliError("--res in the editor needs a level viewport to HighResShot, and a headless editor has "
                               "none: take it during PIE (ue pie start), or in a windowed editor", 4) from None
            raw = None  # a hand-opened editor (minimised, or the viewport tab behind another): see below
        finally:
            ctx.tether("py", code=place.format(*camera[0], *camera[1]))
        if raw is None:  # the shot at the viewport's own size still works there: better than none
            print(f"note: the level viewport took no HighResShot at {args.res}, even after a redraw (is the editor "
                  "minimised, or its viewport tab behind another?); this shot is at the viewport's own size",
                  file=sys.stderr)
            annotations = dict(ANNOTATIONS, gridHeight=args.grid_z) if args.annotate else NO_ANNOTATIONS
            raw = capture_viewport(ctx, pose, annotations, args.ui)
    else:  # editor, or Simulate-In-Editor, whose simulation the level viewport shows
        annotations = dict(ANNOTATIONS, gridHeight=args.grid_z) if args.annotate else NO_ANNOTATIONS
        raw = capture_viewport(ctx, pose, annotations, args.ui)
    return raw


def save_shot(ctx, args, raw, label, pose=None, pie=False, simulate=False):
    """Write a `ue shot` PNG (-o, or Saved/Tether/shots keeping the 50 newest), print it, and handle --baseline and
    --compare."""
    baselines = os.path.join(state_dir(ctx.project), "shots", "baselines")
    width, height = (int.from_bytes(raw[16:20], "big"), int.from_bytes(raw[20:24], "big")) if raw[:4] == b"\x89PNG" else (0, 0)
    if args.output:
        path = os.path.abspath(args.output)
    else:
        shots = os.path.join(state_dir(ctx.project), "shots")
        os.makedirs(shots, exist_ok=True)
        old = sorted(f for f in os.listdir(shots) if f.endswith(".png"))
        for name in old[:-49]:  # keep the 50 newest
            os.remove(os.path.join(shots, name))
        path = stamped_name(shots, "shot", ".png")
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "wb") as f:
        f.write(raw)
    view = getattr(args, "view", None)
    print(f"{path}  {width}x{height}  {len(raw) // 1024} KB" + (f"  ({label})" if label else "  (Simulate)" if simulate else
                                                              "  (PIE, with HUD)" if pie and getattr(args, "hud", False) else "  (PIE)" if pie else "")
          + (f"  view {view}" if view else ""))
    ctx.data = {"path": path, "width": width, "height": height}
    if pie and getattr(args, "hud", False) and width < 1000 and (ctx.beacon or {}).get("headless"):
        print(f"note: the headless editor's PIE viewport is only {width}x{height} (a part of its main window, whose size "
              "the project's saved resolution sets). `ue open --headless --render --size 2560x1440` after a `ue close` "
              "makes it bigger; `ue ui` reads widget texts at any size")
    if pose is None:
        return 0
    loc, rot = pose["location"], pose["rotation"]
    print(f"camera at ({loc['x']:.0f}, {loc['y']:.0f}, {loc['z']:.0f}) "
          f"rot ({rot['pitch']:.0f}, {rot['yaw']:.0f}, {rot['roll']:.0f})")
    if args.baseline:
        os.makedirs(baselines, exist_ok=True)
        with open(os.path.join(baselines, f"{args.baseline}.png"), "wb") as f:
            f.write(raw)
        write_json(os.path.join(baselines, f"{args.baseline}.json"), {"pose": pose, "time": time.time()})
        print(f"baseline '{args.baseline}' saved (ue shot --compare {args.baseline})")
    if args.compare:
        with open(os.path.join(baselines, f"{args.compare}.png"), "rb") as f:
            before = png_read(f.read())
        share, regions, diff = image_diff(before, png_read(raw), args.threshold)
        diff_path = os.path.splitext(path)[0] + f"-diff-{args.compare}.png"
        png_write(diff_path, before[0], before[1], diff)
        print(f"compare '{args.compare}': {share * 100:.2f}% of pixels changed"
              + ("; areas " + ", ".join(f"x {b[0]}-{b[2]} y {b[1]}-{b[3]}" for b in regions[:3])
                 + (f" (+{len(regions) - 3} more)" if len(regions) > 3 else "") if regions else "")
              + f" (tolerance {args.tolerance:g}%)")
        print(f"diff (changes in red): {diff_path}")
        return 1 if share * 100 > args.tolerance else 0
    return 0


def png_read(data):
    """(width, height, channels, pixel bytes) from an 8-bit non-interlaced PNG (stdlib only)."""
    width, height, channels, depth, pixels = png_decode(data)
    if depth != 8:
        raise CliError(f"unsupported PNG (bit depth {depth})")
    return width, height, channels, pixels


def png_decode(data):
    """(width, height, channels, bit depth, raw bytes) from a non-interlaced 8- or 16-bit PNG (16-bit samples
    are big-endian, as PNG stores them)."""
    import struct
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise CliError("not a PNG")
    pos, idat, width = 8, [], 0
    while pos < len(data):
        length, kind = struct.unpack(">I4s", data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + length]
        if kind == b"IHDR":
            width, height, depth, color, _, _, interlace = struct.unpack(">IIBBBBB", body)
            channels = {0: 1, 2: 3, 4: 2, 6: 4}.get(color)
            if depth not in (8, 16) or channels is None or interlace:
                raise CliError(f"unsupported PNG (bit depth {depth}, color type {color}, interlace {interlace})")
        elif kind == b"IDAT":
            idat.append(body)
        elif kind == b"IEND":
            break
        pos += 12 + length
    raw = zlib.decompress(b"".join(idat))
    bpp = channels * depth // 8  # bytes per pixel: the filters' "left" neighbour
    stride = width * bpp
    out = bytearray(stride * height)
    prev = bytearray(stride)
    for y in range(height):
        start = y * (stride + 1)
        kind, line = raw[start], bytearray(raw[start + 1:start + 1 + stride])
        if kind == 1:
            for i in range(bpp, stride):
                line[i] = (line[i] + line[i - bpp]) & 0xFF
        elif kind == 2:
            line = bytearray((a + b) & 0xFF for a, b in zip(line, prev))
        elif kind == 3:
            for i in range(stride):
                left = line[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + ((left + prev[i]) >> 1)) & 0xFF
        elif kind == 4:
            for i in range(stride):
                a = line[i - bpp] if i >= bpp else 0
                b, c = prev[i], (prev[i - bpp] if i >= bpp else 0)
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                line[i] = (line[i] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 0xFF
        out[y * stride:(y + 1) * stride] = line
        prev = line
    return width, height, channels, depth, bytes(out)


def window_png(pids):
    """PNG bytes of the largest visible titled top-level window of these processes (Windows), drawn with
    PrintWindow(PW_RENDERFULLCONTENT), so GPU-rendered content and separate Slate windows such as the movie
    player's loading screen come out, not what happens to be on top of it on screen. (title, png) or None."""
    if os.name != "nt":
        raise CliError("window screenshots are Windows-only for now")
    import ctypes
    from ctypes import wintypes
    user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
    callback = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    found = []

    def visit(hwnd, _):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value in pids and user32.IsWindowVisible(hwnd):
            kind = ctypes.create_unicode_buffer(64)
            user32.GetClassNameW(hwnd, kind, 64)
            if kind.value != "UnrealWindow":  # not the -log console, or other helper windows
                return True
            title = ctypes.create_unicode_buffer(256)
            user32.GetWindowTextW(hwnd, title, 256)
            rect = wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            w, h = rect.right - rect.left, rect.bottom - rect.top
            if w > 50 and h > 50:
                found.append((w * h, hwnd, title.value, w, h))
        return True
    user32.EnumWindows(callback(visit), 0)
    if not found:
        return None
    _, hwnd, title, w, h = max(found)
    gdi32.CreateCompatibleDC.restype = gdi32.CreateCompatibleBitmap.restype = ctypes.c_void_p
    user32.GetWindowDC.restype = ctypes.c_void_p
    screen = user32.GetWindowDC(ctypes.c_void_p(hwnd))
    memdc = gdi32.CreateCompatibleDC(ctypes.c_void_p(screen))
    bitmap = gdi32.CreateCompatibleBitmap(ctypes.c_void_p(screen), w, h)
    try:
        gdi32.SelectObject(ctypes.c_void_p(memdc), ctypes.c_void_p(bitmap))
        user32.PrintWindow(ctypes.c_void_p(hwnd), ctypes.c_void_p(memdc), 2)  # PW_RENDERFULLCONTENT

        class Header(ctypes.Structure):  # BITMAPINFOHEADER
            _fields_ = [("biSize", wintypes.DWORD), ("biWidth", ctypes.c_long), ("biHeight", ctypes.c_long),
                        ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                        ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", ctypes.c_long),
                        ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]
        header = Header(ctypes.sizeof(Header), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)  # top-down BGRA
        buffer = ctypes.create_string_buffer(w * h * 4)
        gdi32.GetDIBits(ctypes.c_void_p(memdc), ctypes.c_void_p(bitmap), 0, h, buffer, ctypes.byref(header), 0)
    finally:
        gdi32.DeleteObject(ctypes.c_void_p(bitmap))
        gdi32.DeleteDC(ctypes.c_void_p(memdc))
        user32.ReleaseDC(ctypes.c_void_p(hwnd), ctypes.c_void_p(screen))
    bgra = buffer.raw
    rgb = bytearray(w * h * 3)
    rgb[0::3], rgb[1::3], rgb[2::3] = bgra[2::4], bgra[1::4], bgra[0::4]
    return title, png_bytes(w, h, bytes(rgb))


def png_gray16_bytes(width, height, values):
    """16-bit greyscale PNG bytes (samples big-endian, as PNG stores them)."""
    import array
    import struct

    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)
    samples = array.array("H", values)
    if sys.byteorder == "little":
        samples.byteswap()
    data, stride = samples.tobytes(), width * 2
    raw = b"".join(b"\x00" + data[y * stride:(y + 1) * stride] for y in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 16, 0, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def flip_rows(width, height, values):
    return [v for y in range(height - 1, -1, -1) for v in values[y * width:(y + 1) * width]]


def read_grid(path):
    """(width, height, max value, values row by row) from a greyscale or colour PNG (8 or 16 bit; colour as its
    luminance, alpha ignored), a raw little-endian .r16, or a raw .u8, the raw ones square."""
    import array
    import math
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError as exc:
        raise CliError(f"cannot read {path}: {exc.strerror}") from None
    ext = os.path.splitext(path)[1].lower()
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        w, h, channels, depth, raw = png_decode(data)
        if depth == 16:
            samples = array.array("H", raw)
            if sys.byteorder == "little":
                samples.byteswap()  # PNG stores 16-bit samples big-endian
        else:
            samples = raw
        if channels == 1:
            values = list(samples)
        elif channels == 2:
            values = list(samples[0::2])
        else:  # RGB(A): Rec. 709 luminance
            r, g, b = samples[0::channels], samples[1::channels], samples[2::channels]
            values = [int(0.2126 * a + 0.7152 * c + 0.0722 * d) for a, c, d in zip(r, g, b)]
        return w, h, 65535 if depth == 16 else 255, values
    if ext in (".r16", ".raw"):
        values = array.array("H", data[: len(data) // 2 * 2])
        if sys.byteorder == "big":
            values.byteswap()
        top = 65535
    elif ext in (".u8", ".r8"):
        values, top = data, 255
    else:
        raise CliError(f"{path}: a PNG, .r16/.raw (16-bit) or .u8 (8-bit) file")
    side = int(math.isqrt(len(values)))
    if side * side != len(values):
        raise CliError(f"{path}: {len(values)} samples is not a square; raw files must be square")
    return side, side, top, list(values)


def grid_to_bytes(w, h, top, values, max_side=512, flip_y=False):
    """A grid as (w, h, one byte per sample 0..255), shrunk by box averaging to at most max_side a side."""
    step = max(1, -(-max(w, h) // max_side))
    ow, oh = -(-w // step), -(-h // step)
    out = bytearray(ow * oh)
    for oy in range(oh):
        for ox in range(ow):
            total = count = 0
            for y in range(oy * step, min(h, (oy + 1) * step)):
                row = y * w
                for x in range(ox * step, min(w, (ox + 1) * step)):
                    total += values[row + x]
                    count += 1
            out[(oh - 1 - oy if flip_y else oy) * ow + ox] = int(round(total / count / top * 255))
    return ow, oh, bytes(out)


def png_bytes(width, height, rgb):
    """8-bit RGB pixels as PNG bytes."""
    import struct

    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)

    stride = width * 3
    raw = b"".join(b"\x00" + rgb[y * stride:(y + 1) * stride] for y in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def png_write(path, width, height, rgb):
    """Write 8-bit RGB pixels as a PNG."""
    with open(path, "wb") as f:
        f.write(png_bytes(width, height, rgb))


def image_diff(before, after, threshold=32):
    """Compare two decoded images: (share of changed pixels, bounding box (x0, y0, x1, y1) or None, diff RGB)."""
    w, h, ca, a = before
    w2, h2, cb, b = after
    if (w, h) != (w2, h2):
        raise CliError(f"sizes differ: baseline {w}x{h}, now {w2}x{h2} (same viewport size needed)")
    changed, box, blocks = 0, None, {}
    diff = bytearray(w * h * 3)
    for y in range(h):
        row_a, row_b = y * w * ca, y * w * cb
        for x in range(w):
            pa, pb = row_a + x * ca, row_b + x * cb
            delta = max(abs(a[pa + k] - b[pb + k]) for k in range(min(ca, cb, 3)))
            o = (y * w + x) * 3
            if delta > threshold:
                changed += 1
                diff[o] = 255
                blocks[(x >> 3, y >> 3)] = blocks.get((x >> 3, y >> 3), 0) + 1
            else:
                grey = (b[pb] + b[pb + (1 if cb > 2 else 0)] + b[pb + (2 if cb > 2 else 0)]) // 9
                diff[o] = diff[o + 1] = diff[o + 2] = grey
    # Changed areas: connected groups of 8x8 blocks at least a quarter changed, so lone noisy pixels (aliasing)
    # do not count, and separate changes (an object moved, clouds drifting) stay apart. Largest first.
    dense, regions = {k for k, n in blocks.items() if n >= 16}, []
    while dense:
        stack, group = [dense.pop()], []
        while stack:
            bx, by = stack.pop()
            group.append((bx, by))
            for n in ((bx + 1, by), (bx - 1, by), (bx, by + 1), (bx, by - 1)):
                if n in dense:
                    dense.remove(n)
                    stack.append(n)
        regions.append((sum(blocks[k] for k in group), (min(k[0] for k in group) * 8, min(k[1] for k in group) * 8,
                        min(max(k[0] for k in group) * 8 + 7, w - 1), min(max(k[1] for k in group) * 8 + 7, h - 1))))
    regions.sort(reverse=True)
    return changed / float(w * h), [r[1] for r in regions], bytes(diff)
