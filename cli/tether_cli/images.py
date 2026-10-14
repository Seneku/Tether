"""Pictures: viewport and PIE screenshots, ue look contact sheets, PNG read/write/diff."""
import json
import os
import re
import time

from .core import CliError, compact_error, editor_mode, log_end, log_path, read_json, state_dir, unwrap, write_json, zlib
from .calls import flush_editor_log


NO_ANNOTATIONS = {"gridSpacing": 0, "gridExtent": 0, "gridHeight": 0, "maxLabelDistance": 0,
                  "classFilter": {"refPath": ""}, "maxLabels": 0}
ANNOTATIONS = {"gridSpacing": 500, "gridExtent": 10000, "gridHeight": 0, "maxLabelDistance": 5000,
               "classFilter": {"refPath": ""}, "maxLabels": 15}


def pie_screenshot(ctx, args):
    """The PIE world as the player sees it. Epic's CaptureViewport renders the *editor* world even during
    PIE (from whatever pose it is given), so take a HighResShot in the PIE world and collect the file."""
    if args.actor or args.at or args.look_at or args.rot or args.annotate:
        raise CliError("during PIE, ue shot shows the player's view; --actor/--at/--look-at/--rot/--annotate "
                       "only work outside PIE, or in Simulate (ue pie start --simulate), which has a free camera")
    if getattr(args, "hud", False):
        return hud_screenshot(ctx)
    start = log_end(ctx.project)
    ctx.tether("console", command="HighResShot 1", pie=True)
    saved = re.compile(r"High resolution screenshot saved as (.+\.png)")
    deadline = time.time() + 15
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
    raise CliError("the PIE screenshot did not appear within 15 s (ue log --level Warning)", 4)


SNAPSHOT_BOX = re.compile(r"^(\s*)(\w+).*?\[pos=(-?\d+),(-?\d+) size=(\d+),(\d+)\]")


def hud_rect(snapshot, view_w, view_h):
    """(x, y, w, h) of the game viewport inside the editor window, in window units, from a Slate inspector
    snapshot: the smallest panel as wide as the viewport and at least as tall (the viewport tab's area), with
    the viewport at its bottom, below the tab's toolbar. None when no panel fits (PIE in its own window)."""
    boxes = []
    for line in snapshot.splitlines():
        m = SNAPSHOT_BOX.match(line)
        if m:
            boxes.append(tuple(int(v) for v in m.groups()[2:]))
    # The viewport's panel: as wide as the viewport (give or take a border), taller by its tab and toolbar.
    fits = [b for b in boxes if abs(b[2] - view_w) <= 4 and view_h - 4 <= b[3] < view_h + 600]
    if not fits:
        return None
    x, y, w, h = min(fits, key=lambda b: b[3])
    return x + (w - view_w) / 2, y + h - view_h, view_w, view_h


def png_crop(raw, rect, scale=1.0):
    """PNG bytes of a rectangle (window units, times scale for pixels) of a PNG."""
    width, height, channels, pixels = png_read(raw)
    x0, y0 = max(0, round(rect[0] * scale)), max(0, round(rect[1] * scale))
    x1, y1 = min(width, round((rect[0] + rect[2]) * scale)), min(height, round((rect[1] + rect[3]) * scale))
    rows = []
    for y in range(y0, y1):
        line = pixels[(y * width + x0) * channels:(y * width + x1) * channels]
        rows.append(line if channels == 3 else bytes(b for i, b in enumerate(line) if i % channels < 3))
    return png_bytes(x1 - x0, y1 - y0, b"".join(rows))


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
        image_w = int.from_bytes(raw[16:20], "big")
        return png_crop(raw, cache["rect"], image_w / cache["window_w"])
    text, is_error = ctx.mcp.call("SlateInspectorToolset.SlateInspectorToolset", "Snapshot",
                                  {"ref": "", "maxDepth": 60, "bIncludeSourceLocations": False})
    snapshot = unwrap(text) if not is_error else ""
    snapshot = snapshot if isinstance(snapshot, str) else json.dumps(snapshot)
    window = SNAPSHOT_BOX.match(snapshot.splitlines()[0]) if snapshot else None
    rect = hud_rect(snapshot, view_w, view_h) if snapshot else None
    if rect is None:
        print("note: could not place the game viewport in the window (Slate inspector unavailable, or PIE runs in "
              "its own window): this is the whole window")
        return raw
    image_w = int.from_bytes(raw[16:20], "big")
    window_w = int(window.group(5)) if window and int(window.group(5)) else image_w
    scale = image_w / window_w  # DPI scaling
    if cache is not None:
        cache.update(rect=rect, window_w=window_w)
    return png_crop(raw, rect, scale)


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
          "s": "000011010001110"}


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
    width, height, rgb = contact_sheet(tiles, columns=3, tile_width=args.tile)
    if args.output:
        path = os.path.abspath(args.output)
    else:
        folder = os.path.join(state_dir(ctx.project), "shots")
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, f"look-{args.asset.rstrip('/').rsplit('/', 1)[-1].split('.')[0]}-"
                                    f"{time.strftime('%Y%m%d-%H%M%S')}.png")
    png_write(path, width, height, rgb)
    print(f"{path}  {width}x{height}")
    print(f"{args.asset} ({stage.get('class')}, about {stage['radius'] * 2 / 100:.1f} m across): "
          "top row front, right, back; bottom row left, top, three-quarter")
    return 0


def shot_args(**given):
    """Arguments for calling cmd_shot from other commands: every option at its default, then `given`. A unit
    test keeps these in step with the `shot` parser, so a new option cannot break the callers again."""
    defaults = dict(actor=None, at=None, look_at=None, rot=None, annotate=False, grid_z=0, ui=False, hud=False,
                    output=None, baseline=None, compare=None, tolerance=0.5, threshold=32)
    import argparse
    return argparse.Namespace(**dict(defaults, **given))


def cmd_shot(ctx, args):
    """Capture the viewport (or the player's view during PIE) to a PNG and print its path."""
    launch = editor_mode(ctx.project, ctx.beacon)
    if launch.get("headless") and not launch.get("render"):
        raise CliError("this editor runs headless with -nullrhi and cannot render; "
                       "reopen it with `ue open --headless --render` (or windowed)")
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
    if getattr(args, "hud", False) and not (pie and not simulate):
        raise CliError("--hud needs PIE (ue pie start): it shows the game's UMG HUD over the player's view")
    if pie and not simulate:  # Play: CaptureViewport would render the editor world, so HighResShot the game
        raw = pie_screenshot(ctx, args)
    else:  # editor, or Simulate-In-Editor, whose simulation the level viewport shows
        annotations = dict(ANNOTATIONS, gridHeight=args.grid_z) if args.annotate else NO_ANNOTATIONS
        raw = capture_viewport(ctx, pose, annotations, args.ui)
    width, height = (int.from_bytes(raw[16:20], "big"), int.from_bytes(raw[20:24], "big")) if raw[:4] == b"\x89PNG" else (0, 0)
    if args.output:
        path = os.path.abspath(args.output)
    else:
        shots = os.path.join(state_dir(ctx.project), "shots")
        os.makedirs(shots, exist_ok=True)
        old = sorted(f for f in os.listdir(shots) if f.endswith(".png"))
        for name in old[:-49]:  # keep the 50 newest
            os.remove(os.path.join(shots, name))
        path = os.path.join(shots, time.strftime("shot-%Y%m%d-%H%M%S.png"))
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "wb") as f:
        f.write(raw)
    loc, rot = pose["location"], pose["rotation"]
    print(f"{path}  {width}x{height}  {len(raw) // 1024} KB" + ("  (Simulate)" if simulate else
                                                              "  (PIE, with HUD)" if pie and getattr(args, "hud", False) else "  (PIE)" if pie else ""))
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
            if depth != 8 or channels is None or interlace:
                raise CliError(f"unsupported PNG (bit depth {depth}, color type {color}, interlace {interlace})")
        elif kind == b"IDAT":
            idat.append(body)
        elif kind == b"IEND":
            break
        pos += 12 + length
    raw = zlib.decompress(b"".join(idat))
    stride = width * channels
    out = bytearray(stride * height)
    prev = bytearray(stride)
    for y in range(height):
        start = y * (stride + 1)
        kind, line = raw[start], bytearray(raw[start + 1:start + 1 + stride])
        if kind == 1:
            for i in range(channels, stride):
                line[i] = (line[i] + line[i - channels]) & 0xFF
        elif kind == 2:
            line = bytearray((a + b) & 0xFF for a, b in zip(line, prev))
        elif kind == 3:
            for i in range(stride):
                left = line[i - channels] if i >= channels else 0
                line[i] = (line[i] + ((left + prev[i]) >> 1)) & 0xFF
        elif kind == 4:
            for i in range(stride):
                a = line[i - channels] if i >= channels else 0
                b, c = prev[i], (prev[i - channels] if i >= channels else 0)
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                line[i] = (line[i] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 0xFF
        out[y * stride:(y + 1) * stride] = line
        prev = line
    return width, height, channels, bytes(out)


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
