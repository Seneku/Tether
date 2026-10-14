"""Content and world editing: ue import, unused, lint, landscape."""
import fnmatch
import json
import os
import re
import time

from .core import CliError, filter_log, human_bytes, log_end, log_path, state_dir
from .calls import flush_editor_log
from .images import png_write
from .assets import SKIP_DIRS, SOURCE_EXTS, package_bytes, project_code_roots

IMPORT_EXTS = {
    "mesh": (".fbx", ".obj", ".glb", ".gltf", ".usd", ".usda", ".usdc", ".usdz", ".abc"),
    "texture": (".png", ".tga", ".jpg", ".jpeg", ".exr", ".hdr", ".bmp", ".tif", ".tiff", ".psd", ".dds"),
    "sound": (".wav", ".ogg", ".flac", ".aif", ".aiff"),
}
IMPORTABLE = tuple(ext for exts in IMPORT_EXTS.values() for ext in exts)


LOD_NODE = re.compile(rb"([A-Za-z0-9_]+?)_LOD(\d+)(?=\x00\x01Model|::|\"|\x00)")


def lod_named_meshes(path, limit=64 << 20):
    """{base name: sorted LOD numbers} for meshes inside an FBX/glTF named Name_LOD0, Name_LOD1... (Unity's
    convention). Interchange does not read those as LODs: it stacks them into one LOD of one mesh."""
    if not path.lower().endswith((".fbx", ".gltf", ".glb")):
        return {}
    try:
        with open(path, "rb") as f:
            data = f.read(limit)
    except OSError:
        return {}
    found = {}
    for m in LOD_NODE.finditer(data):
        found.setdefault(m.group(1).decode("utf-8", "replace"), set()).add(int(m.group(2)))
    return {base: sorted(lods) for base, lods in found.items() if len(lods) > 1}


def import_plan(sources, dest):
    """{content folder: [files]} for files and folders; a folder's subfolders become subfolders of dest."""
    plan = {}
    dest = dest.rstrip("/")
    for source in sources:
        source = os.path.abspath(source)
        if os.path.isfile(source):
            plan.setdefault(dest, []).append(source)
        elif os.path.isdir(source):
            for dirpath, dirs, files in os.walk(source):
                dirs[:] = sorted(d for d in dirs if not d.startswith("."))
                rel = os.path.relpath(dirpath, source)
                folder = dest if rel == "." else dest + "/" + rel.replace(os.sep, "/")
                picked = sorted(os.path.join(dirpath, f) for f in files if f.lower().endswith(IMPORTABLE))
                if picked:
                    plan.setdefault(folder, []).extend(picked)
        else:
            raise CliError(f"no such file or folder: {source}")
    if not plan:
        raise CliError("nothing importable there (" + ", ".join(sorted(e.lstrip(".") for e in IMPORTABLE)) + ")")
    return plan


def _import_warnings(ctx, start, limit=8):
    flush_editor_log(ctx)
    with open(log_path(ctx.project), "rb") as f:
        f.seek(start)
        lines = filter_log(f.read().decode("utf-8", "replace").splitlines(), level="Warning")
    return lines[:limit], max(0, len(lines) - limit)


def cmd_import(ctx, args):
    """Import files without dialogs, apply mesh/texture settings, report what came in."""
    start = log_end(ctx.project)
    if args.reimport:
        rows = ctx.tether("reimport_assets", paths=json.dumps(args.paths), force=args.force, save=args.save)
        rows = json.loads(rows) if isinstance(rows, str) else rows
        by_state = {}
        for row in rows:
            by_state.setdefault(row["state"], []).append(row)
        for row in by_state.get("reimported", []):
            print(f"reimported {row['path']}  {row['detail']}")
        for row in by_state.get("failed", []):
            print(f"FAILED     {row['path']}  from {row['source']}")
        for row in by_state.get("source missing", []):
            print(f"missing    {row['path']}  (source {row['source']} is gone)")
        quiet = [f"{len(by_state[s])} {s}" for s in ("unchanged", "unsaved", "no source") if by_state.get(s)]
        print(f"{len(by_state.get('reimported', []))} reimported" + (", " + ", ".join(quiet) if quiet else "")
              + ("" if args.force or not by_state.get("unchanged") else " (--force reimports unchanged ones too)"))
        code = 1 if by_state.get("failed") else 0
        ctx.data = rows
    else:
        if len(args.paths) < 2:
            raise CliError("ue import <file|folder> ... <dest>, e.g. ue import Art/Crate.fbx /Game/Props")
        *sources, dest = args.paths
        if not dest.startswith("/"):
            raise CliError(f"the last argument is the content folder to import into (/Game/...), not '{dest}'")
        plan = import_plan(sources, dest)
        if args.name and sum(len(files) for files in plan.values()) > 1:
            raise CliError("--name works with a single file")
        for files in plan.values():
            for path in files:
                for base, lods in lod_named_meshes(path).items():
                    print(f"warning: {os.path.basename(path)} has meshes {base}_LOD{lods[0]}..{lods[-1]} (Unity's LOD naming): "
                          f"Interchange imports them stacked into a single LOD. Rename them LOD{lods[0]}_{base}... or put "
                          "them in an FBX LOD group to get one mesh with LODs")
        options = {"name": args.name, "collision": args.collision, "lods": args.lods, "nanite": args.nanite,
                   "texture_group": args.texture_group, "replace": not args.no_replace, "save": args.save}
        results, code = [], 0
        for folder, files in plan.items():
            out = ctx.tether("import_files", files=json.dumps(files), dest=folder,
                             options=json.dumps({k: v for k, v in options.items() if v is not None}))
            results += json.loads(out) if isinstance(out, str) else out
        count = 0
        for result in results:
            if result["error"]:
                code = 1
                print(f"FAILED {os.path.basename(result['file'])}: {result['error']}")
            for asset in result["assets"]:
                count += 1
                print(f"+ {asset['path']}  {asset['class']}" + (f"  {asset['detail']}" if asset["detail"] else ""))
        print(f"{count} asset(s) from {len(results)} file(s)" + ("" if args.save else ", not saved yet (ue save, or --save)"))
        ctx.data = results
    warnings, more = _import_warnings(ctx, start)
    if warnings:
        print("import log:")
        for line in warnings:
            print("  " + line)
        if more:
            print(f"  ... +{more} more (ue log --since start)")
    return code


GAME_PATH = re.compile(r"/Game/[\w/\-]+(?:\.[\w\-]+)?")
ROOT_DIR_LINE = re.compile(r"DirectoriesToAlwaysCook|PrimaryAssetTypesToScan|DirectoriesToNeverCook")
ROOT_DIR = re.compile(r'Path="(/Game/[^"]*)"')


def unused_roots(project):
    """Roots the asset registry cannot see: /Game paths the config and code name, and the folders the
    Asset Manager scans or always cooks (as 'dir:' entries)."""
    roots, folders = set(), set()
    for top in project_code_roots(project):
        for dirpath, dirs, files in os.walk(top):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            for name in files:
                if not name.endswith(SOURCE_EXTS + (".ini",)):
                    continue
                try:
                    with open(os.path.join(dirpath, name), encoding="utf-8", errors="replace") as f:
                        for line in f:
                            if "/Game/" not in line:
                                continue
                            if name.endswith(".ini") and ROOT_DIR_LINE.search(line):
                                if "NeverCook" not in line:
                                    folders.update(ROOT_DIR.findall(line))
                                continue
                            roots.update(m.split(".")[0] for m in GAME_PATH.findall(line))
                except OSError:
                    continue
    return sorted(roots) + sorted("dir:" + f for f in folders)


def cmd_unused(ctx, args):
    """Assets nothing reaches from the maps, config, code and cook settings, biggest first; redirectors."""
    if args.redirectors:
        data = ctx.tether("unused", path=args.path, roots="[]")
        found = data["redirectors"]
        if not found:
            print(f"no redirectors under {args.path}")
            return 0
        if not args.fix:
            print(f"{len(found)} redirector(s) under {args.path} (ue unused --redirectors --fix resaves their "
                  "referencers and deletes them):")
            for package in found[: args.limit]:
                print("  " + package)
            ctx.data = found
            return 0
        out = ctx.tether("fix_redirectors", paths=json.dumps(found))
        print(out)
        return 1 if any(not line.split(": ", 1)[-1].startswith("fixed") for line in out.splitlines()) else 0
    roots = unused_roots(ctx.project)
    data = ctx.tether("unused", path=args.path, roots=json.dumps(roots))
    rows = [(package, cls, package_bytes(ctx.project, package) or 0) for package, cls in data["unused"]]
    rows.sort(key=lambda r: -r[2])
    total = sum(r[2] for r in rows)
    root_text = ", ".join(f"{n} {kind}" for kind, n in data["roots"].items() if n)
    print(f"{len(rows)} of {data['total']} asset(s) under {args.path} are unused, {human_bytes(total)} on disk"
          f" (reached from: {root_text or 'nothing'})")
    for package, cls, size in rows[: args.top]:
        print(f"  {human_bytes(size):>9}  {package}  {cls}")
    if len(rows) > args.top:
        print(f"  ... +{len(rows) - args.top} more (--top N)")
    folders = {}
    for package, _, size in rows:
        folder = package.rsplit("/", 1)[0]
        count, total_size = folders.get(folder, (0, 0))
        folders[folder] = (count + 1, total_size + size)
    if len(folders) > 1:
        print("by folder:")
        for folder, (count, size) in sorted(folders.items(), key=lambda kv: -kv[1][1])[:8]:
            print(f"  {human_bytes(size):>9}  {folder}  ({count})")
    if data["redirectors"]:
        print(f"{len(data['redirectors'])} redirector(s) too (ue unused --redirectors)")
    if rows:
        print("note: an asset loaded only by a path built at runtime looks unused here; `ue usages <asset>` "
              "before deleting")
    ctx.data = {"unused": [{"package": p, "class": c, "bytes": b} for p, c, b in rows],
                "redirectors": data["redirectors"], "roots": data["roots"]}
    return 0


# Prefixes by asset class (Epic's and the community style guide's); a project can change them in
# Config/Tether/lint.json: {"prefixes": {"World": ["L_", "Lvl_"]}, "ignore": ["/Game/Fab/*"], "disable": ["naming"],
# "max_texture_size": 4096, "lod_triangles": 10000}.
LINT_PREFIXES = {
    "StaticMesh": ["SM_"], "SkeletalMesh": ["SK_", "SKM_"], "Skeleton": ["SKEL_"], "PhysicsAsset": ["PHYS_", "PA_"],
    "Texture2D": ["T_"], "TextureCube": ["T_", "HDR_"], "TextureRenderTarget2D": ["RT_"],
    "Material": ["M_"], "MaterialInstanceConstant": ["MI_"], "MaterialFunction": ["MF_"],
    "MaterialParameterCollection": ["MPC_"], "Blueprint": ["BP_"], "WidgetBlueprint": ["WBP_", "W_"],
    "AnimBlueprint": ["ABP_"], "AnimSequence": ["A_", "AS_"], "AnimMontage": ["AM_"], "BlendSpace": ["BS_"],
    "BlendSpace1D": ["BS_"], "SoundWave": ["S_", "SW_"], "SoundCue": ["SC_"], "MetaSoundSource": ["MS_", "MSS_"],
    "SoundAttenuation": ["ATT_", "SA_"], "NiagaraSystem": ["NS_"], "NiagaraEmitter": ["NE_"], "DataTable": ["DT_"],
    "CurveTable": ["CT_"], "CurveFloat": ["Curve_", "C_"], "UserDefinedEnum": ["E_", "Enum_"],
    "UserDefinedStruct": ["F_", "S_", "ST_"], "InputAction": ["IA_"], "InputMappingContext": ["IMC_"],
    "BehaviorTree": ["BT_"], "BlackboardData": ["BB_"], "StateTree": ["ST_"], "World": ["L_", "Lvl_", "Map_"],
    "LevelSequence": ["LS_", "SEQ_"], "PhysicalMaterial": ["PM_", "PhysMat_"], "Font": ["Font_", "F_"],
}
NORMAL_NAME = re.compile(r"_(N|Normal|Nrm)$", re.I)
MASK_NAME = re.compile(r"_(ORM|ARM|MRA|RMA|ORD|Masks?|AO|Rough(ness)?|Metal(lic)?|Height)$", re.I)


TEXTURE_CLASSES = ("Texture2D", "TextureCube", "Texture2DArray", "VolumeTexture")
FORMAT_BITS = {  # bits per pixel by pixel format (the registry's Format tag, PF_ dropped)
    "DXT1": 4, "BC1": 4, "BC4": 4, "ETC2_RGB": 4, "DXT3": 8, "DXT5": 8, "BC2": 8, "BC3": 8, "BC5": 8, "BC6H": 8,
    "BC7": 8, "ETC2_RGBA": 8, "ASTC_4x4": 8, "ASTC_6x6": 4, "ASTC_8x8": 2, "G8": 8, "A8": 8, "R8": 8, "L8": 8,
    "G16": 16, "R16F": 16, "R16_UINT": 16, "V8U8": 16, "R8G8": 16, "B8G8R8A8": 32, "R8G8B8A8": 32, "A2B10G10R10": 32,
    "FloatR11G11B10": 32, "R32_FLOAT": 32, "R32F": 32, "G16R16": 32, "G16R16F": 32, "FloatRGBA": 64,
    "A16B16G16R16": 64, "R16G16B16A16_UNORM": 64, "A32B32G32R32F": 128,
}
UNCOMPRESSED_FORMATS = {f for f, bits in FORMAT_BITS.items() if bits >= 16 and not f.startswith(("BC", "DXT", "ETC", "ASTC"))}


def texture_format(tag):
    return (tag or "").replace("PF_", "")


# Compression settings that store uncompressed on purpose (HDR, float data, displacement, editor icons).
UNCOMPRESSED_ON_PURPOSE = {"TC_HDR", "TC_HDR_F32", "TC_HalfFloat", "TC_SingleFloat", "TC_VectorDisplacementmap",
                           "TC_DistanceFieldFont", "TC_EditorIcon", "TC_HDR_Compressed", "TC_LQ"}


def texture_size(tags):
    """(width, height) from the Dimensions tag ('2048x2048'; arrays and volume textures add a third number)."""
    parts = (tags.get("Dimensions") or "0x0").split("x")
    try:
        return int(parts[0]), int(parts[1])
    except (ValueError, IndexError):
        return 0, 0


def texture_bytes(w, h, tags):
    """GPU memory at full resolution (capped by its max size) with its mip chain (a third more): x6 for a cube,
    x slices or depth for arrays and volumes."""
    cap = int(tags.get("MaxTextureSize") or 0)
    if cap and max(w, h) > cap:
        scale = cap / max(w, h)
        w, h = max(1, int(w * scale)), max(1, int(h * scale))
    depth = (tags.get("Dimensions") or "").split("x")[2:3]
    size = w * h * FORMAT_BITS.get(texture_format(tags.get("Format")), 8) / 8
    size *= int(depth[0]) if depth and depth[0].isdigit() else 1
    pow2 = w and h and not (w & (w - 1) or h & (h - 1))
    if pow2 and tags.get("MipGenSettings") != "TMGS_NoMipmaps":
        size *= 4 / 3
    return int(size * (6 if tags.get("_class") == "TextureCube" else 1))


def lint_rules(rows, config=None):
    """[(rule, package, message)] for `ue lint`, from lint_data rows and the project's lint.json."""
    config = config or {}
    prefixes = dict(LINT_PREFIXES, **config.get("prefixes", {}))
    disabled, ignore = set(config.get("disable", [])), config.get("ignore", [])
    max_size, lod_tris = config.get("max_texture_size", 4096), config.get("lod_triangles", 10000)
    found = []

    def add(rule, package, message):
        if rule not in disabled:
            found.append((rule, package, message))
    for package, cls, tags in rows:
        if package.startswith(("/Game/Developers/", "/Game/Collections/")) or \
                any(fnmatch.fnmatch(package, pattern) for pattern in ignore):
            continue
        name = package.rsplit("/", 1)[-1]
        wanted = prefixes.get(cls)
        if wanted and not name.startswith(tuple(wanted)):
            add("naming", package, f"{cls}: expected {' or '.join(wanted)}")
        if " " in name or not name.isascii():
            add("characters", package, "spaces or non-ASCII characters in the name break some platforms and tools")
        if cls in TEXTURE_CLASSES and tags.get("Dimensions"):
            w, h = texture_size(tags)
            ui = "UI" in (tags.get("LODGroup") or "")
            if not ui and w and h and (w & (w - 1) or h & (h - 1)):
                add("texture-pow2", package, f"{w}x{h} is not a power of two: no mips, no streaming")
            if max(w, h) > max_size:
                add("texture-size", package, f"{w}x{h} is over {max_size}")
            if NORMAL_NAME.search(name) and tags.get("CompressionSettings") not in (None, "TC_Normalmap"):
                add("texture-normal", package, f"named as a normal map but compressed as {tags['CompressionSettings']}")
            if MASK_NAME.search(name) and tags.get("SRGB") == "True":
                add("texture-mask-srgb", package, "a mask/packed texture with sRGB on (its values get gamma-curved)")
            fmt = texture_format(tags.get("Format"))
            if not ui and fmt in UNCOMPRESSED_FORMATS and w * h >= 512 * 512                     and tags.get("CompressionSettings") not in UNCOMPRESSED_ON_PURPOSE:
                add("texture-uncompressed", package, f"{w}x{h} stored as {fmt} ({human_bytes(texture_bytes(w, h, tags))}, "
                                                     f"{tags.get('CompressionSettings') or 'TC_Default'})")
            if tags.get("NeverStream") == "True" and max(w, h) >= 1024 and tags.get("VirtualTextureStreaming") != "True":
                add("texture-never-stream", package, f"{w}x{h} never streams: all its mips stay in memory")
        if cls == "StaticMesh" and tags.get("Triangles"):
            tris = int(tags["Triangles"])
            if tags.get("CollisionPrims") == "0" and tags.get("CollisionComplexity") != "CTF_UseComplexAsSimple" \
                    and tags.get("DefaultCollision") != "NoCollision":
                add("mesh-collision", package, f"no simple collision ({tags.get('DefaultCollision') or 'default'} "
                                               "profile): characters and physics pass through it")
            if tags.get("NaniteEnabled") != "True" and tags.get("LODs") == "1" and tris > lod_tris:
                add("mesh-lods", package, f"{tris} triangles, one LOD and no Nanite")
    return found


LINT_FIXES = {"texture-normal": "normalmap", "texture-mask-srgb": "linear", "mesh-collision": "box"}


def lint_plan(found, rules=None):
    """[{rule, package, fix}] for the findings that have a safe fix. Renames (naming, characters) only when their
    rule is asked for by name: they touch every asset that references the one renamed."""
    plan = []
    for rule, package, message in found:
        name = package.rsplit("/", 1)[-1]
        if rule == "naming" and rules and "naming" in rules:
            prefix = re.search(r"expected (\w+?)(?: or|$)", message)
            if prefix:
                plan.append({"rule": rule, "package": package, "fix": f"rename:{prefix.group(1)}{name}"})
        elif rule == "characters" and rules and "characters" in rules:
            clean = re.sub(r"[^A-Za-z0-9_]+", "_", name).strip("_")
            if clean and clean != name:
                plan.append({"rule": rule, "package": package, "fix": f"rename:{clean}"})
        elif rule in LINT_FIXES:
            plan.append({"rule": rule, "package": package, "fix": LINT_FIXES[rule]})
    return plan


def lint_config(ctx):
    """(path, settings) of the project's Config/Tether/lint.json ({} when there is none)."""
    config_path = os.path.join(os.path.dirname(ctx.project), "Config", "Tether", "lint.json")
    config = {}
    if os.path.isfile(config_path):
        try:
            with open(config_path, encoding="utf-8") as f:
                config = json.load(f)
        except ValueError as exc:
            raise CliError(f"{config_path}: {exc}")
    return config_path, config


def texture_rows(rows, config=None):
    """`ue tex` rows from lint_data rows: [{package, class, w, h, format, bytes, group, flags, issues}], largest first."""
    out = []
    for package, cls, tags in rows:
        if cls not in TEXTURE_CLASSES:
            continue
        tags = dict(tags, _class=cls)
        w, h = texture_size(tags)
        flags = [f for f, on in (("sRGB", tags.get("SRGB") == "True"), ("never streams", tags.get("NeverStream") == "True"),
                                 ("virtual", tags.get("VirtualTextureStreaming") == "True"),
                                 ("alpha", tags.get("HasAlphaChannel") == "True"),
                                 ("no mips", tags.get("MipGenSettings") == "TMGS_NoMipmaps")) if on]
        if tags.get("MaxTextureSize") not in (None, "0"):
            flags.append(f"max {tags['MaxTextureSize']}")
        issues = [(rule, message) for rule, _, message in lint_rules([[package, cls, tags]], config)
                  if rule.startswith("texture-")]
        out.append({"package": package, "class": cls, "w": w, "h": h, "format": texture_format(tags.get("Format")) or "?",
                    "compression": tags.get("CompressionSettings") or "",
                    "group": (tags.get("LODGroup") or "").replace("TEXTUREGROUP_", ""),
                    "bytes": texture_bytes(w, h, tags), "flags": flags, "issues": issues})
    out.sort(key=lambda r: -r["bytes"])
    return out


def texture_line(row):
    return (f"{human_bytes(row['bytes']):>9}  {row['w']}x{row['h']} {row['format']} {row['group']}"
            + (f" ({', '.join(row['flags'])})" if row["flags"] else "") + f"  {row['package']}")


def tex_streaming(ctx, args):
    data = ctx.tether("texture_streaming", limit=args.top)
    data = json.loads(data) if isinstance(data, str) else data
    pool = data["pool_mb"]
    print(f"{data['count']} textures in memory: {data['in_mem_mb']:.0f} MB ({data['streaming_mb']:.0f} MB streaming"
          + (f", {100 * data['streaming_mb'] / pool:.0f}% of the {pool} MB pool" if pool else "")
          + f"; {data['non_streaming_mb']:.0f} MB never streams)")
    if data["wanting_count"]:
        full = pool and data["streaming_mb"] + data["wanting_mb"] > pool
        print(f"{data['wanting_count']} want more mips than they have (~{data['wanting_mb']:.0f} MB more): "
              + ("over the pool, so the streamer is holding them back" if full else "still streaming in"))
    print("largest in memory:")
    for t in data["largest"]:
        print(f"  {t['kb'] / 1024:>7.1f} MB  {t['current']} of {t['max']} {t['format']} {t['group']}"
              + ("" if t["streaming"] else " (never streams)") + (" (virtual)" if t["vt"] else "") + f"  {t['path']}")
    if data["wanting"]:
        print("wanting more:")
        for w in data["wanting"]:
            print(f"  {w['current']} -> {w['wanted']}" + (f" (+{w['missing_kb'] / 1024:.1f} MB)" if w["missing_kb"] else "")
                  + f"  {w['path']}")
    ctx.data = data
    return 0


def cmd_tex(ctx, args):
    """Texture audit from registry tags (sizes, formats, estimated memory, issues), or the live streaming pool."""
    if args.streaming:
        return tex_streaming(ctx, args)
    path = (args.path or "/Game").split(".")[0].rstrip("/") or "/Game"
    _, config = lint_config(ctx)
    if args.max:
        config["max_texture_size"] = args.max

    def textures_under(folder):
        rows = ctx.tether("lint_data", path=folder)
        return texture_rows(json.loads(rows) if isinstance(rows, str) else rows, config)
    textures = textures_under(path)
    parent = path.rsplit("/", 1)[0]
    exact = [t for t in textures_under(parent) if t["package"] == path] if not textures and parent else []
    if exact:
        t = exact[0]
        print(f"{t['package']} ({t['class']}): {t['w']}x{t['h']} {t['format']}, {t['compression']}, group {t['group']}, "
              f"~{human_bytes(t['bytes'])} at full resolution" + (f"; {', '.join(t['flags'])}" if t["flags"] else ""))
        for rule, message in t["issues"]:
            print(f"  {rule}: {message}")
        ctx.data = t
        return 1 if t["issues"] and args.strict else 0
    total = sum(t["bytes"] for t in textures)
    streamed = sum(t["bytes"] for t in textures if "never streams" not in t["flags"] and "virtual" not in t["flags"])
    issues = {}
    for t in textures:
        for rule, message in t["issues"]:
            issues.setdefault(rule, []).append((t, message))
    print(f"{len(textures)} textures under {path}: ~{human_bytes(total)} at full resolution with mips"
          + (f" ({human_bytes(total - streamed)} of it never streams or is virtual)" if total - streamed else "")
          + (f"; {sum(len(v) for v in issues.values())} issue(s)" if issues else ""))
    if not textures:
        return 0
    print("largest:")
    for t in textures[: args.top]:
        print("  " + texture_line(t))
    per_rule = min(args.limit, 6)
    for rule, items in sorted(issues.items(), key=lambda kv: -len(kv[1])):
        print(f"{rule} ({len(items)}):")
        for t, message in items[:per_rule]:
            print(f"  {t['package']}  {message}")
        if len(items) > per_rule:
            print(f"  ... +{len(items) - per_rule} more (ue lint {path} --rule {rule})")
    ctx.data = {"total_bytes": total, "streamed_bytes": streamed, "count": len(textures),
                "issues": {rule: len(items) for rule, items in issues.items()}, "textures": textures[: args.limit]}
    return 1 if issues and args.strict else 0


def cmd_lint(ctx, args):
    """Content rules beyond Epic's validators, from asset registry tags (nothing is loaded)."""
    config_path, config = lint_config(ctx)
    if args.ignore:
        config["ignore"] = config.get("ignore", []) + args.ignore
    rows = ctx.tether("lint_data", path=args.path)
    rows = json.loads(rows) if isinstance(rows, str) else rows
    found = lint_rules(rows, config)
    if args.rule:
        found = [f for f in found if re.search(args.rule, f[0])]
    if args.fix:
        rules = {f[0] for f in found} if args.rule else None
        plan = lint_plan(found, rules)
        if not plan:
            print("nothing to fix" + ("" if args.rule else " (renames need --rule naming or --rule characters)"))
            return 0
        if args.dry_run:
            for item in plan[: args.limit]:
                print(f"would fix {item['package']}: {item['fix'].replace('rename:', 'rename to ')}")
            print(f"{len(plan)} fix(es) planned" + (f", first {args.limit} shown" if len(plan) > args.limit else ""))
            ctx.data = plan
            return 0
        results = ctx.tether("lint_fix", plan=json.dumps(plan), save=args.save)
        results = json.loads(results) if isinstance(results, str) else results
        failed = [r for r in results if not r["ok"]]
        for r in results[: args.limit]:
            print(f"{'fixed ' if r['ok'] else 'FAILED'} {r['package']}: {r['detail']}")
        print(f"{len(results) - len(failed)} fixed, {len(failed)} failed" + ("" if args.save else
                                                                           "; not saved (ue save, or --save)"))
        ctx.data = results
        return 1 if failed else 0
    by_rule = {}
    for rule, package, message in found:
        by_rule.setdefault(rule, []).append((package, message))
    ranked = sorted(by_rule.items(), key=lambda kv: -len(kv[1]))
    print(f"{len(rows)} asset(s) under {args.path}: "
          + (", ".join(f"{len(items)} {rule}" for rule, items in ranked) if ranked else "nothing to report")
          + (f" (rules from {os.path.relpath(config_path, os.path.dirname(ctx.project))})" if config else ""))
    per_rule = args.limit if args.rule else min(args.limit, 8)
    for rule, items in ranked:
        print(f"{rule}:")
        for package, message in items[:per_rule]:
            print(f"  {package}  {message}")
        if len(items) > per_rule:
            print(f"  ... +{len(items) - per_rule} more (ue lint --rule {rule} --limit N)")
    ctx.data = [{"rule": r, "package": p, "message": m} for r, p, m in found]
    return 1 if found and args.strict else 0


LAYER_COLOURS = [(110, 160, 70), (140, 130, 120), (190, 170, 120), (90, 120, 160), (170, 110, 80), (200, 200, 200)]


def landscape_png(grid, path):
    """A height/layer picture of a landscape grid: brightness is height, colour is the dominant paint layer."""
    px, heights, dominant, layers = grid["px"], grid["heights"], grid["dominant"], grid["layers"]
    values = [v for row in heights for v in row if v is not None]
    lo, hi = (min(values), max(values)) if values else (0, 1)
    span = (hi - lo) or 1.0
    rgb = bytearray()
    for row in reversed(range(px)):  # north (+y) up
        for col in range(px):
            z = heights[row][col]
            if z is None:
                rgb += bytes((255, 0, 255))
                continue
            shade = 0.35 + 0.65 * (z - lo) / span
            layer = dominant[row][col]
            base = LAYER_COLOURS[layers.index(layer) % len(LAYER_COLOURS)] if layer in layers else (255, 255, 255)
            rgb += bytes(min(255, int(c * shade)) for c in base)
    png_write(path, px, px, bytes(rgb))
    return lo, hi


def cmd_landscape(ctx, args):
    """Landscape: summary, the surface at a point, a height/layer picture, or sculpting a circle."""
    import math
    nums = [float(v) for v in (args.where or "").replace(" ", "").split(",") if v] if args.where else []
    if args.action == "info":
        print(ctx.tether("landscape", action="info", x=0.0, y=0.0, size=0.0, px=2))
        return 0
    if args.action == "at":
        if len(nums) != 2:
            raise CliError("ue landscape at x,y")
        data = ctx.tether("landscape", action="at", x=nums[0], y=nums[1], size=0.0, px=2)
        data = json.loads(data) if isinstance(data, str) else data
        weights = ", ".join(f"{k} {v:g}" for k, v in data["weights"].items() if v) or "no paint layers"
        print(f"{data['landscape']} at {data['x']}, {data['y']}: z {data['z']}, slope {data['slope']} deg; {weights}")
        ctx.data = data
        return 0
    if args.action == "map":
        if len(nums) not in (0, 3):
            raise CliError("ue landscape map [x,y,size]  (default: the whole landscape)")
        x, y, size = nums if nums else (0.0, 0.0, 0.0)
        grid = ctx.tether("landscape", action="grid", x=x, y=y, size=size, px=args.px)
        grid = json.loads(grid) if isinstance(grid, str) else grid
        path = args.output or os.path.join(state_dir(ctx.project), "shots", time.strftime("landscape-%Y%m%d-%H%M%S.png"))
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        lo, hi = landscape_png(grid, path)
        legend = ", ".join(f"{name} {LAYER_COLOURS[i % len(LAYER_COLOURS)]}" for i, name in enumerate(grid["layers"]))
        print(f"{path}  ({grid['size'] / 100:.0f} m square around {grid['x']:.0f}, {grid['y']:.0f}; z {lo:.0f} to {hi:.0f}, "
              f"brighter is higher, north up" + (f"; colours: {legend}" if legend else "") + ")")
        ctx.data = {"png": path, "z_min": lo, "z_max": hi}
        return 0
    if args.action == "restore":
        if not args.backup:
            raise CliError("ue landscape restore --backup <file> (each edit prints its backup)")
        x = y = radius = 0.0
    else:
        if len(nums) != 3:
            raise CliError(f"ue landscape {args.action} x,y,radius")
        x, y, radius = nums
    value = {"flatten": args.height if args.height is not None else -1e30, "raise": args.by or 0.0}.get(args.action, 0.0)
    strength = args.strength if args.strength is not None else (2.0 if args.action == "smooth" else 1.0)
    tool = lambda step: ctx.tether("landscape_edit", step=step, action=args.action, x=x, y=y, radius=radius,  # noqa: E731
                                   value=value, strength=strength,
                                   backup=os.path.abspath(args.backup) if args.backup else "-")
    tool("begin")
    try:
        time.sleep(0.3)  # the export renders on a later frame
        applied = tool("apply")
        time.sleep(0.3)  # and so does the patch
        result = tool("commit")
    except CliError:
        tool("cancel")
        raise
    result = json.loads(result) if isinstance(result, str) else result
    applied = json.loads(applied) if isinstance(applied, str) else applied
    if not result.get("ok"):
        raise CliError("the landscape refused the heightmap import (ue log)")
    if args.action == "restore":
        print(f"restored {result['vertices']} vertices from {args.backup}")
    else:
        now, deadline = None, time.time() + 5
        while time.time() < deadline:  # collision follows the heightmap a frame or two later
            at = ctx.tether("landscape", action="at", x=x, y=y, size=0.0, px=2)
            now = (json.loads(at) if isinstance(at, str) else at)["z"]
            if abs(now - result["centre_before"]) > 0.5 or not result["max_change_cm"]:
                break
            time.sleep(0.25)
        print(f"{args.action}: centre z {result['centre_before']} -> {now}, largest change {result['max_change_cm']} cm "
              f"over {result['vertices']} vertices; undo with ue landscape restore --backup {applied['backup']}")
    ctx.data = result
    return 0
