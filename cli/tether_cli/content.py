"""Content and world editing: ue import, unused, lint, landscape."""
import fnmatch
import json
import os
import re
import sys
import time

from .core import CliError, editor_live, filter_log, human_bytes, log_end, log_path, state_dir
from .share import file_owner
from .transport import py_json
from .calls import flush_editor_log
from .images import flip_rows, grid_to_bytes, png_gray16_bytes, png_write, read_grid
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
                if asset["class"] in ("StaticMesh", "SkeletalMesh"):  # what DCC exports get wrong, right away
                    try:
                        check = ctx.tether("mesh_check", target=asset["path"], height=-1.0)
                        check = json.loads(check) if isinstance(check, str) else check
                        for issue in check["issues"]:
                            print(f"    ! {issue}")
                    except CliError:
                        pass
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
        if tags.get("_missing"):
            add("missing-ref", package, f"references {len(tags['_missing'])} package(s) that do not exist: "
                                        + ", ".join(tags["_missing"][:3]) + " (they load as None)")
        if cls == "SoundWave" and tags.get("_attenuated") and int(tags.get("NumChannels") or 1) > 1:
            add("sound-stereo-3d", package, f"{tags['NumChannels']} channels with attenuation: a 3D sound should be mono "
                                            "(stereo plays from both speakers, spread only by its stereo spread)")
        if cls in ("StaticMesh", "SkeletalMesh") and tags.get("_materials") == 0:
            add("mesh-no-material", package, "depends on no material: its slots are empty (renders with the default "
                                             "material) or point at assets that are gone")
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


CPP_NOISE = re.compile(r'//[^\n]*|/\*.*?\*/|"(?:\\.|[^"\\\n])*"|\'(?:\\.|[^\'\\\n])*\'', re.S)
CPP_TYPE_DECL = re.compile(r"^(?:template\s*<[^>]*>\s*)?(?:struct|class|enum(?:\s+class)?|union)\s+(?:\w+_API\s+)?(\w+)")
CPP_NAME = re.compile(r"\b(\w+)\s*(?:\(|=|\[|$|\{)")


def file_scope_names(source):
    """Names a .cpp declares that are private to it: in an anonymous namespace, `static` at file scope, or types
    defined at file scope. A unity build (or Live Coding regrouping files) compiles several .cpp files as one, and
    then two of these with the same name collide."""
    text = CPP_NOISE.sub(lambda m: " " if m.group(0).startswith(("/", '"', "'")) else m.group(0), source)
    text = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    names, depth, anon, stmt = [], 0, [], ""
    for ch in text:
        if ch in "{;":
            head = re.sub(r"\s+", " ", stmt).strip()
            stmt = ""
            private = bool(anon and depth == anon[-1])
            if ch == "{" and re.fullmatch(r"(?:inline\s+)?namespace", head):
                anon.append(depth + 1)
            elif ch == "{" and head.startswith("namespace"):
                pass  # a named namespace: its contents are not file-private
            elif head and (private or depth == 0) and not head.startswith(("using ", "extern ", "friend ")):
                decl = CPP_TYPE_DECL.match(head)
                if decl and ch == "{":
                    names.append(decl.group(1))
                elif (private or head.startswith(("static ", "static\t"))) and not decl:
                    body = re.split(r"[=(\[{]", head, maxsplit=1)
                    m = re.search(r"(\w+)\s*$", body[0])
                    if m and m.group(1) not in ("operator", "const", "constexpr", "static", "auto"):
                        names.append(m.group(1))
            if ch == "{":
                depth += 1
        elif ch == "}":
            if anon and depth == anon[-1]:
                anon.pop()
            depth = max(0, depth - 1)
            stmt = ""
        elif depth == 0 or (anon and depth == anon[-1]):
            stmt += ch
    return names


def cpp_collisions(project):
    """{module: {name: [files]}} for file-private names declared in more than one .cpp of the same module."""
    found = {}
    for root in project_code_roots(project):
        if os.path.basename(root) != "Source":
            continue
        for dirpath, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            build = [f for f in files if f.endswith(".Build.cs")]
            if not build:
                continue
            module = build[0][: -len(".Build.cs")]
            seen = {}
            for here, subdirs, names in os.walk(dirpath):
                subdirs[:] = [d for d in subdirs if d not in SKIP_DIRS]
                for f in names:
                    if f.endswith(".cpp"):
                        path = os.path.join(here, f)
                        with open(path, encoding="utf-8", errors="replace") as fh:
                            for name in set(file_scope_names(fh.read())):
                                seen.setdefault(name, []).append(os.path.relpath(path, dirpath))
            clashes = {n: sorted(p) for n, p in seen.items() if len(p) > 1}
            if clashes:
                found[module] = clashes
            dirs[:] = []  # a module's own subfolders were walked above
    return found


REFLECTED_TYPE = re.compile(r"\b(UCLASS|USTRUCT|UINTERFACE|UENUM)\s*\([^)]*\)\s*(?:class|struct|enum(?:\s+class)?)\s+"
                            r"(?:\w+_API\s+)?([AUFIES])(\w+)")


def reflected_name_clashes(project):
    """{engine name: [(type, file)]} for reflected types whose names differ only by their prefix (FSDSetPiece and
    ASDSetPiece are both SDSetPiece to UHT, which refuses the second)."""
    seen = {}
    for root in project_code_roots(project):
        for dirpath, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            for f in files:
                if not f.endswith(".h"):
                    continue
                path = os.path.join(dirpath, f)
                with open(path, encoding="utf-8", errors="replace") as fh:
                    for m in REFLECTED_TYPE.finditer(fh.read()):
                        bare = m.group(3)
                        seen.setdefault(bare, set()).add((m.group(2) + bare, os.path.relpath(path, os.path.dirname(project))))
    return {name: sorted(types) for name, types in seen.items() if len({t for t, _ in types}) > 1}


BIND_KEY = re.compile(r"\bBindKey\s*\(\s*(FInputChord\s*\(\s*)?EKeys::(\w+)([^;]*)")
CHORD_FLAGS = ("Shift", "Ctrl", "Alt", "Cmd")  # FInputChord(Key, bShift, bCtrl, bAlt, bCmd)


def bind_keys(project):
    """[(chord, key, file, line)] of the legacy BindKey calls in the project's C++ ("Ctrl+G", "G", ...)."""
    found = []
    for root in project_code_roots(project):
        for dirpath, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            for f in files:
                if not f.endswith((".cpp", ".h")):
                    continue
                path = os.path.join(dirpath, f)
                with open(path, encoding="utf-8", errors="replace") as fh:
                    text = fh.read()
                for m in BIND_KEY.finditer(text):
                    key, mods = m.group(2), []
                    if m.group(1):  # the chord's flags follow the key, up to its closing parenthesis
                        flags = re.sub(r"/\*.*?\*/", "", m.group(3).split(")")[0]).split(",")[1:]
                        mods = [name for name, flag in zip(CHORD_FLAGS, flags) if flag.strip() == "true"]
                    found.append(("+".join(mods + [key]), key, os.path.relpath(path, os.path.dirname(project)),
                                  text.count("\n", 0, m.start()) + 1))
    return found


IMC_KEYS = """import json
out = {}
for a in unreal.AssetRegistryHelpers.get_asset_registry().get_assets_by_class(
        unreal.TopLevelAssetPath("/Script/EnhancedInput", "InputMappingContext"), True):
    path = str(a.package_name)
    if not path.startswith("/Game"):
        continue
    imc = unreal.load_asset(path)
    try:
        maps = imc.get_editor_property("default_key_mappings").get_editor_property("mappings")
    except Exception:
        maps = imc.get_editor_property("mappings")
    for m in maps:
        text = m.get_editor_property("key").export_text()
        key = text.split('KeyName="')[1].split('"')[0] if 'KeyName="' in text else text.strip('()"')
        action = m.get_editor_property("action")
        out.setdefault(key, []).append([path.rsplit("/", 1)[-1], action.get_name() if action else ""])
print(json.dumps(out))
"""


def imc_keys_offline(project, keys):
    """{key: [(IMC, "")]} for the given key names found in the name tables of the project's input mapping contexts
    (assets named IMC_*), read from the .uasset files: no editor needed, no action names."""
    out = {}
    content = os.path.join(os.path.dirname(project), "Content")
    for dirpath, dirs, files in os.walk(content) if os.path.isdir(content) else []:
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and d != "__ExternalActors__"]
        for f in files:
            if not (f.startswith("IMC_") and f.endswith(".uasset")):
                continue
            with open(os.path.join(dirpath, f), "rb") as fh:
                data = fh.read(1 << 20)
            for key in keys:  # an FName in the name map: int32 length (with the terminator), the chars, NUL
                if (len(key) + 1).to_bytes(4, "little") + key.encode() + b"\0" in data:
                    out.setdefault(key, []).append((f[:-7], ""))
    return out


def bind_key_conflicts(ctx, binds):
    """[(chord, file, line, [(IMC, action)])] for BindKey keys that an input mapping context also maps, and
    where the mappings came from ("the editor" or "the IMC_ .uasset files")."""
    keys = {key for _, key, _, _ in binds}
    if not keys:
        return [], None
    if editor_live(ctx.project):
        mapped, source = py_json(ctx, IMC_KEYS), "the editor's input mapping contexts"
    else:
        mapped, source = imc_keys_offline(ctx.project, keys), "the IMC_ .uasset files (start the editor for all of them)"
    return [(chord, path, line, mapped[key]) for chord, key, path, line in binds if key in mapped], source


def lint_cpp(ctx, args):
    """`ue lint --cpp`: unity-build collisions and UHT name clashes waiting to happen, from the sources alone."""
    clashes = reflected_name_clashes(ctx.project)
    print(f"{len(clashes)} reflected type name(s) used twice once the prefix is dropped" + (
        " (UHT knows types without A/U/F/I/E/S, so the second fails the build): rename one" if clashes else ""))
    for name, types in sorted(clashes.items())[: args.limit]:
        print(f"  {name}: " + ", ".join(f"{t} ({file_owner_label(ctx, path)})" for t, path in types))
    found = cpp_collisions(ctx.project)
    total = sum(len(v) for v in found.values())
    print(f"{total} file-private name(s) declared in more than one .cpp of a module" + (
        ": a unity build (or Live Coding regrouping changed files) compiles them as one file and they collide; "
        "rename one, or move it into a named namespace" if total else ""))
    for module, clashes in found.items():
        print(f"{module}:")
        for name, files in sorted(clashes.items())[: args.limit]:
            print(f"  {name}: {', '.join(files[:4])}" + (f" +{len(files) - 4} more" if len(files) > 4 else ""))
    binds = bind_keys(ctx.project)
    conflicts, source = bind_key_conflicts(ctx, binds)
    if binds:
        print(f"{len(conflicts)} of {len(binds)} BindKey key(s) also mapped by an input mapping context (from {source})"
              + (": Enhanced Input consumes a mapped key, so the BindKey never fires; use another key, or an "
                 "input action" if conflicts else ""))
        for chord, path, line, where in conflicts[: args.limit]:
            maps = ", ".join(sorted({f"{imc}" + (f" ({action})" if action else "") for imc, action in where}))
            print(f"  {chord} at {path.replace(os.sep, '/')}:{line}: mapped in {maps}")
    ctx.data = {"collisions": found, "reflected_clashes": clashes,
                "bind_key_conflicts": [{"chord": c, "file": p, "line": n, "mapped": w} for c, p, n, w in conflicts]}
    return 1 if (total or clashes or conflicts) and args.strict else 0


def file_owner_label(ctx, rel):
    """'Robots/SDSetPiece.h, edited by objectives 5 min ago' style: where a file is and who last wrote it."""
    owner = file_owner(ctx.project, os.path.join(os.path.dirname(ctx.project), rel))
    return rel.replace("\\", "/") + (f", {owner}" if owner else "")


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
    if getattr(args, "cpp", False):
        return lint_cpp(ctx, args)
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


FAILED_MATERIAL = re.compile(r"((?:[A-Za-z]:)?[\\/][^:\[\]]*?\.uasset): Failed to compile Material for platform")


def failed_materials(ctx):
    """Content paths of the materials the editor failed to compile this session (each falls back to the default
    material in game): from its log, which is the only place that says."""
    try:
        flush_editor_log(ctx)
        with open(log_path(ctx.project), encoding="utf-8", errors="replace") as f:
            text = f.read()
    except (OSError, CliError):
        return []
    content = os.path.join(os.path.dirname(ctx.project), "Content")
    found = []
    for m in FAILED_MATERIAL.finditer(text):
        path = os.path.normpath(m.group(1).strip())
        rel = os.path.relpath(path, content) if os.path.normcase(path).startswith(os.path.normcase(content)) else None
        name = "/Game/" + os.path.splitext(rel)[0].replace("\\", "/") if rel else os.path.basename(path)
        if name not in found:
            found.append(name)
    return found


USAGE_FLAG = re.compile(r"Material (/\S+?)(?:\.\S+)? needed to set usage flag (\w+)")


def usage_flagged_materials(ctx):
    """[(material, usage)] the editor set a missing usage flag on this session (bUsedWithInstancedStaticMeshes and
    the like): set at render time, it dirties the material and lasts only if saved."""
    try:
        with open(log_path(ctx.project), encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return []
    return list(dict.fromkeys((m.group(1), m.group(2)) for m in USAGE_FLAG.finditer(text)))


def cmd_foliage(ctx, args):
    """Foliage instances: list by type, add over an area or a mask, remove; check, convert actors, fix."""
    import base64

    def opt(value):
        return "-" if value is None else value
    mask = ""
    if args.mask:
        w, h, top, values = read_grid(args.mask)
        mw, mh, pixels = grid_to_bytes(w, h, top, values, flip_y=args.flip_y)
        grid = {"w": mw, "h": mh, "data": base64.b64encode(pixels).decode("ascii")}
        if args.mask_area:
            rect = [float(v) for v in args.mask_area.split(",")]
            if len(rect) != 4:
                raise CliError("--mask-area x0,y0,x1,y1 (cm)")
            grid["rect"] = rect
        mask = json.dumps(grid)
    avoid = []
    for rule in args.avoid or []:
        rx, _, radius = rule.rpartition(":")
        try:
            avoid.append([rx or radius, float(radius) if rx else 500.0])
        except ValueError:
            raise CliError(f"--avoid REGEX:RADIUS_CM (not '{rule}')") from None
    if args.action == "fix" and args.what not in ("billboards", "cull", "wpo"):
        raise CliError("ue foliage fix billboards | cull --distance N | wpo --distance N")
    if args.action == "check":
        print(ctx.tether("foliage", action="check", what=opt(args.what), area="-", density=-1.0, count=-1,
                         spacing=-1.0, slope="-", height="-", scale="-", align=False, seed=0, folder=args.folder,
                         mask="", avoid="", value=-1.0, scope=""))
        failed, flagged = failed_materials(ctx), usage_flagged_materials(ctx)
        if failed:
            print(f"! {len(failed)} material(s) failed to compile this session and render as the default grey material: "
                  + ", ".join(failed[:8]) + (" ..." if len(failed) > 8 else "") + " (ue log --grep 'Failed to compile')")
        if flagged:
            usages = sorted({u for _, u in flagged})
            names = list(dict.fromkeys(m for m, _ in flagged))
            print(f"! {len(names)} material(s) lacked a usage flag ({', '.join(usages)}) that the editor set this session, "
                  "which left them unsaved: save them (ue save), or the next session sets it again and a cooked game "
                  "renders those meshes with the default material: " + ", ".join(n.rsplit('/', 1)[-1] for n in names[:8])
                  + (" ..." if len(names) > 8 else ""))
        return 0
    print(ctx.tether("foliage", action=args.action, what=opt(args.what), area=opt(args.area),
                     density=-1.0 if args.density is None else args.density, count=-1 if args.count is None else args.count,
                     spacing=-1.0 if args.spacing is None else args.spacing, slope=opt(args.slope),
                     height=opt(args.height), scale=opt(args.scale), align=args.align, seed=args.seed,
                     folder=args.folder, mask=mask, avoid=json.dumps(avoid) if avoid else "",
                     value=-1.0 if args.distance is None else args.distance, scope=args.scope or ""))
    return 0


WATER = """
import json
_editor = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem)
w = _editor.get_game_world() or _editor.get_editor_world()  # during PIE the editor world does not answer
def get(obj, name, default=None):
    try:
        return obj.get_editor_property(name)
    except Exception:
        return default
def path(obj):
    return obj.get_path_name().split(".")[0] if obj else None
actors = unreal.GameplayStatics.get_all_actors_of_class(w, unreal.Actor)
zones, bodies = [], []
WaterZone, WaterBody = getattr(unreal, "WaterZone", None), getattr(unreal, "WaterBody", None)
for a in actors if WaterZone else []:  # no Water plugin: no water classes
    if isinstance(a, WaterZone):
        ext, res = get(a, "zone_extent"), get(a, "render_target_resolution")
        local = get(a, "local_tessellation_extent")
        zones.append({"label": a.get_actor_label(), "extent": [ext.x, ext.y] if ext else None,
                      "resolution": [res.x, res.y] if res else None,
                      "local": bool(get(a, "enable_local_only_tessellation", False)),
                      "local_extent": [local.x, local.y] if local else None})
    elif isinstance(a, WaterBody):
        c = a.get_water_body_component() if hasattr(a, "get_water_body_component") else None
        bodies.append({"label": a.get_actor_label(), "class": a.get_class().get_name(), "z": a.get_actor_location().z,
                       "affects_landscape": bool(get(c, "affects_landscape", False)) if c else None,
                       "material": path(get(c, "water_material")) if c else None,
                       "lod_material": path(get(c, "water_static_mesh_material")) if c else None,
                       "waves": bool(get(a, "water_waves"))})
import math, re
floats = []
Buoyancy = getattr(unreal, "BuoyancyComponent", None)
for a in actors if Buoyancy else []:
    for c in a.get_components_by_class(Buoyancy):
        data = get(c, "buoyancy_data")
        text = data.export_text() if data is not None else ""
        number = lambda f, d=0.0: float(re.search(f + r"=(-?[\\d.]+)", text).group(1)) if re.search(f + r"=(-?[\\d.]+)", text) else d
        radii = [float(p.get_editor_property("radius")) for p in (get(data, "pontoons", []) or [])] if data is not None else []
        root = a.root_component
        body = get(root, "body_instance") if root else None
        floats.append({"label": a.get_actor_label(), "radii": radii, "coefficient": number("BuoyancyCoefficient"),
                       "max_force": number("MaxBuoyantForce", 0.0),
                       "overlaps_at_load": bool(get(a, "generate_overlap_events_during_level_streaming", False)),
                       "mass": (root.get_mass() if hasattr(root, "get_mass") else None) if root else None,
                       "simulates": bool(get(body, "simulate_physics", False)) if body is not None else None})
landscapes = len(unreal.GameplayStatics.get_all_actors_of_class(w, unreal.LandscapeProxy))
print(json.dumps({"zones": zones, "bodies": bodies, "landscapes": landscapes, "plugin": WaterZone is not None,
                  "floats": floats}))
"""
WATER_SHORE_M = 0.5  # metres per water-info texel above which the shoreline fade shows as slivers


def cmd_water(ctx, args):
    """`ue water`: water zones and bodies as text, with the checks that cost IslandHorror time: a body that edits the
    landscape, a coarse shoreline (zone extent / water-info texture), a stock material left on the distant LOD mesh."""
    data = py_json(ctx, WATER)
    ctx.data = data
    zones, bodies = data["zones"], data["bodies"]
    if not data.get("plugin", True):
        print("the Water plugin is not enabled in this project")
        return 0
    if not zones and not bodies and not data.get("floats"):
        print("no water in the edited level (WaterZone, WaterBody actors)")
        return 0
    issues = 0
    for z in zones:
        line = f"zone {z['label']}"
        if z["extent"]:
            line += f": {z['extent'][0] * 2 / 100:.0f} x {z['extent'][1] * 2 / 100:.0f} m"
        texel = None
        if z["extent"] and z["resolution"] and z["resolution"][0]:
            texel = z["extent"][0] * 2 / 100 / z["resolution"][0]
            line += f", water-info {z['resolution'][0]} x {z['resolution'][1]} ({texel:.2f} m/texel)"
        line += (f", local tessellation {z['local_extent'][0] / 100:.0f} m around the camera" if z["local"] and z["local_extent"]
                 else ", local tessellation off")
        print(line)
        if texel and texel > WATER_SHORE_M and not z["local"]:
            issues += 1
            print(f"  ! the shoreline fade is coarse ({texel:.2f} m/texel) and shows as flickering slivers: set "
                  "enable_local_only_tessellation and local_tessellation_extent (about 40000), or raise "
                  "render_target_resolution")
    for b in bodies:
        print(f"{b['class']} {b['label']}: z {b['z']:.0f}" + (", waves" if b["waves"] else "")
              + (f", material {b['material'].rsplit('/', 1)[-1]}" if b["material"] else "")
              + (f", distant mesh {b['lod_material'].rsplit('/', 1)[-1]}" if b["lod_material"] else ""))
        if b["affects_landscape"] and data["landscapes"]:
            issues += 1
            print("  ! it edits the landscape (affects_landscape): its brush flattens or carves the terrain under it. "
                  "If the heightmap already has the shore, set affects_landscape False on its water body component")
        if b["material"] and b["lod_material"] and not b["material"].startswith("/Water/") \
                and b["lod_material"].startswith("/Water/"):
            issues += 1
            print("  ! the near water has its own material but the distant LOD mesh still uses the stock one "
                  "(water_static_mesh_material): far water will not match")
    if bodies and not zones:
        issues += 1
        print("! water bodies but no WaterZone: nothing renders them")
    for f in data.get("floats") or []:
        issues += buoyancy_lines(f)
    print(f"{len(zones)} zone(s), {len(bodies)} body(ies)"
          + (f", {len(data['floats'])} buoyant actor(s)" if data.get("floats") else "") + f", {issues} issue(s)")
    return 1 if issues else 0


GRAVITY = 980.0  # cm/s^2


def buoyancy_lines(f):
    """Print one buoyant actor and its problems; return how many. Lift is shared across pontoons (their coefficients
    come from ComputeSprungMasses with a mass of 1, so they sum to 1): fully under water, the total is about
    BuoyancyCoefficient x one pontoon's volume (cm^3), each pontoon capped at MaxBuoyantForce."""
    import math
    radii = f["radii"]
    sizes = ", ".join(f"{radii.count(r)} x r {r:.3g}" if radii.count(r) > 1 else f"r {r:.3g}" for r in dict.fromkeys(radii))
    line = (f"buoyant {f['label']}: {len(radii)} pontoon(s)" + (f" ({sizes} cm)" if radii else "")
            + f", coefficient {f['coefficient']:g}" + (f", mass {f['mass']:.0f} kg" if f.get("mass") else ""))
    problems = []
    if not f["overlaps_at_load"]:
        problems.append("generate overlap events during level streaming is off: an actor placed in the level gets no "
                        "begin-overlap at load, so it never enters the water and sinks (Epic's BP_BuoyancyExample "
                        f"does this too): ue set {f['label']} bGenerateOverlapEventsDuringLevelStreaming=true (or in "
                        "its Blueprint's class defaults)")
    if f.get("simulates") is False:
        problems.append("its root does not simulate physics: buoyancy pushes nothing")
    if radii and f.get("mass"):
        volume = sum(4 / 3 * math.pi * r ** 3 for r in radii) / len(radii)  # coefficients near-equal: the mean
        lift = volume * f["coefficient"]
        if f.get("max_force"):
            lift = min(lift, f["max_force"])
        weight = f["mass"] * GRAVITY
        line += f"; most lift ~{lift / weight:.2f}x its weight"
        if lift < weight:
            needed = 2 * weight / volume  # floating half under water
            problems.append(f"it cannot float: fully under water it lifts about {lift / weight:.2f}x its weight. Lift is "
                            "shared across pontoons (total ~ coefficient x one pontoon's volume), so per-pontoon tuning "
                            f"sinks it; a coefficient around {needed:.2g} floats it about half under")
    print(line)
    for p in problems:
        print("  ! " + p)
    return len(problems)


def landscape_file(ctx, args):
    """`ue landscape export|import <file>` and `paint <layer> --mask <file>`: heights and paint-layer weights in
    and out of the existing landscape, through a render target (so the editor must render). Heightmaps are the
    landscape's own raw values (32768 = the actor's z), rows along +Y and columns along +X, as Unreal imports
    them; --flip-y for files whose first row is +Y."""
    import array
    if (ctx.beacon or {}).get("nullrhi"):
        raise CliError("this editor runs without rendering (-nullrhi), and heightmaps go through a render target: "
                       "ue close, then ue open --headless --render (or use a windowed editor)")
    work = os.path.join(state_dir(ctx.project), "landscape")
    os.makedirs(work, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")

    def tool(step, action, file="", layer=""):
        out = ctx.tether("landscape_edit", step=step, action=action, x=0.0, y=0.0, radius=0.0, value=0.0,
                         strength=1.0, backup="-", file=file, layer=layer)
        return json.loads(out) if isinstance(out, str) else out

    if args.action == "export":
        if not args.where:
            raise CliError("ue landscape export <file.png|.r16>")
        target = os.path.abspath(args.where)
        raw = target if target.lower().endswith(".r16") and not args.flip_y else os.path.join(work, f"export-{stamp}.r16")
        tool("begin", "export", raw)
        try:
            time.sleep(0.3)  # the export renders on a later frame
            result = tool("apply", "export")
        except CliError:
            tool("cancel", "export")
            raise
        w, h = result["vertices"]
        if raw != target:
            values = array.array("H")
            with open(raw, "rb") as f:
                values.frombytes(f.read())
            if sys.byteorder == "big":
                values.byteswap()
            values = flip_rows(w, h, list(values)) if args.flip_y else list(values)
            if target.lower().endswith(".png"):
                data = png_gray16_bytes(w, h, values)
            else:
                out = array.array("H", values)
                if sys.byteorder == "big":
                    out.byteswap()
                data = out.tobytes()
            with open(target, "wb") as f:
                f.write(data)
            os.remove(raw)
        print(f"{target}: {w} x {h} vertices, z {result['z_min']} to {result['z_max']} (16-bit, 32768 = the "
              "landscape's z" + (", flipped: first row is +Y" if args.flip_y else ", first row is -Y") + ")")
        ctx.data = dict(result, file=target)
        return 0
    if args.action == "import":
        if not args.where:
            raise CliError("ue landscape import <file.png|.r16>  (16-bit, exactly the landscape's vertex grid)")
        w, h, top, values = read_grid(args.where)
        if top != 65535:
            raise CliError(f"{args.where} is 8-bit: a heightmap needs 16 bits (256 levels would terrace the terrain)")
        if args.flip_y:
            values = flip_rows(w, h, values)
        raw = os.path.join(work, f"import-{stamp}.r16")
        data = array.array("H", values)
        if sys.byteorder == "big":
            data.byteswap()
        with open(raw, "wb") as f:
            data.tofile(f)
        tool("begin", "import", raw)
        try:
            time.sleep(0.3)
            applied = tool("apply", "import")
            time.sleep(0.3)  # the patch draws on a later frame
            result = tool("commit", "import")
        except CliError:
            tool("cancel", "import")
            raise
        finally:
            if os.path.exists(raw):
                os.remove(raw)
        if not result.get("ok"):
            raise CliError("the landscape refused the heightmap import (ue log)")
        change = ""
        grid = result.get("grid")
        try:
            old = read_grid(applied["backup"])[3]
        except CliError:
            old, grid = None, None
        if grid and old and len(old) == len(values):
            k = max(range(len(values)), key=lambda i: abs(values[i] - old[i]))
            cm = (values[k] - old[k]) / 128.0 * grid["scale"][2]
            x, y = grid["min_x"] + (k % w) * grid["scale"][0], grid["min_y"] + (k // w) * grid["scale"][1]
            goal = grid["z0"] + (values[k] - 32768) / 128.0 * grid["scale"][2]
            deadline = time.time() + 5
            while abs(cm) > 1 and time.time() < deadline:  # collision follows the heightmap a frame or two later
                at = ctx.tether("landscape", action="at", x=x, y=y, size=0.0, px=2)
                if abs((json.loads(at) if isinstance(at, str) else at)["z"] - goal) < 2:
                    break
                time.sleep(0.25)
            change = f"; largest change {cm:+.0f} cm at ({x:.0f}, {y:.0f})" if abs(cm) >= 1 else "; no height changed"
        print(f"imported {w} x {h} vertices, z {result['z_min']} to {result['z_max']}{change}; undo: ue undo, or "
              f"ue landscape import {applied['backup']}")
        ctx.data = result
        return 0
    if not args.where or not args.mask:
        raise CliError("ue landscape paint <layer> --mask <picture>  (white = full weight)")
    w, h, top, values = read_grid(args.mask)
    if args.flip_y:
        values = flip_rows(w, h, values)
    grey = bytes(int(round(v / top * 255)) for v in values)
    picture = os.path.join(work, f"paint-{stamp}.png")
    png_write(picture, w, h, bytes(b for g in grey for b in (g, g, g)))
    try:
        tool("begin", "paint", picture, args.where)
        time.sleep(0.3)  # the picture draws on a later frame
        result = tool("commit", "paint", layer=args.where)
    except CliError:
        tool("cancel", "paint")
        raise
    finally:
        os.remove(picture)
    if not result.get("ok"):
        raise CliError("the landscape refused the weightmap import (ue log)")
    print(f"painted layer {args.where} from {args.mask} ({w} x {h}) over {result['vertices']} vertices; "
          + (f"undo: ue landscape paint {args.where} --mask {result['backup']}" if result.get("backup")
             else "undo: Edit > Undo in the editor"))
    ctx.data = result
    return 0


def cmd_landscape(ctx, args):
    """Landscape: summary, the surface at a point, a height/layer picture, or sculpting a circle."""
    import math
    if args.action in ("export", "import", "paint"):
        return landscape_file(ctx, args)
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
                                   backup=os.path.abspath(args.backup) if args.backup else "-", file="", layer="")
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


def cmd_mesh(ctx, args):
    """`ue mesh check <mesh|folder>...`: sanity checks for meshes from DCC tools."""
    targets = []
    for path in args.paths:
        rows = ctx.tether("lint_data", path=path.rstrip("/")) if "." not in path.rsplit("/", 1)[-1] else []
        rows = json.loads(rows) if isinstance(rows, str) else rows
        meshes = [r[0] for r in rows if r[1] in ("StaticMesh", "SkeletalMesh")]
        targets += meshes or [path]
    flagged = 0
    for target in targets[: args.limit]:
        data = ctx.tether("mesh_check", target=target, height=args.height or -1.0)
        data = json.loads(data) if isinstance(data, str) else data
        flagged += bool(data["issues"])
        print(f"{data['mesh']} ({data['class']}): {data['size'][0]:g} x {data['size'][1]:g} x {data['size'][2]:g} cm, "
              f"pivot {data['pivot']}")
        for fact in data["facts"]:
            print(f"  {fact}")
        for issue in data["issues"]:
            print(f"  ! {issue}")
    if len(targets) > 1:
        print(f"{len(targets)} mesh(es) checked, {flagged} with issues" + (f" (first {args.limit}; --limit N)"
                                                                           if len(targets) > args.limit else ""))
    return 1 if flagged and args.strict else 0


# 3x5 pixel digits for numbering markers on `ue map` (stdlib only: no fonts).
DIGITS = {"0": "111101101101111", "1": "010110010010111", "2": "111001111100111", "3": "111001111001111",
          "4": "101101111001001", "5": "111100111001111", "6": "111100111101111", "7": "111001010010010",
          "8": "111101111101111", "9": "111101111001111"}
MAP_COLOURS = [((230, 60, 60), "red"), ((60, 160, 255), "blue"), ((250, 210, 40), "yellow"), ((80, 210, 90), "green"),
               ((220, 90, 230), "magenta"), ((255, 140, 30), "orange"), ((60, 225, 220), "cyan"), ((255, 255, 255), "white"),
               ((150, 110, 70), "brown"), ((160, 160, 255), "lavender")]


class Canvas:
    """An RGB picture to draw a map on, y down."""

    def __init__(self, size, colour=(36, 38, 42)):
        self.size = size
        self.px = bytearray(bytes(colour) * size * size)

    def put(self, x, y, colour):
        if 0 <= x < self.size and 0 <= y < self.size:
            i = (y * self.size + x) * 3
            self.px[i:i + 3] = bytes(colour)

    def rect(self, x0, y0, x1, y1, fill, edge=None):
        for y in range(max(0, y0), min(self.size, y1 + 1)):
            for x in range(max(0, x0), min(self.size, x1 + 1)):
                border = edge and (x in (x0, x1) or y in (y0, y1))
                self.put(x, y, edge if border else fill)

    def dot(self, x, y, colour, r=4):
        for dy in range(-r - 1, r + 2):
            for dx in range(-r - 1, r + 2):
                d = dx * dx + dy * dy
                if d <= r * r:
                    self.put(x + dx, y + dy, colour)
                elif d <= (r + 1) * (r + 1):
                    self.put(x + dx, y + dy, (0, 0, 0))

    def number(self, x, y, text, colour=(255, 255, 255)):
        for k, ch in enumerate(text):
            bits = DIGITS.get(ch)
            for i, bit in enumerate(bits or ""):
                if bit == "1":
                    for sy in range(2):
                        for sx in range(2):
                            self.put(x + k * 8 + (i % 3) * 2 + sx, y + (i // 3) * 2 + sy, colour)


def map_picture(data, size, centre, half, grid=None, numbers=True):
    """(canvas, legend rows, numbered markers) for `ue map`: x right, y up, `half` cm from the centre to an edge."""
    canvas = Canvas(size)
    scale = size / (2 * half)

    def to_px(x, y):
        return int((x - (centre[0] - half)) * scale), int(((centre[1] + half) - y) * scale)
    if grid:  # landscape: brightness is height
        heights = [v for row in grid["heights"] for v in row if v is not None]
        lo, hi = (min(heights), max(heights)) if heights else (0, 1)
        n = grid["px"]
        for py in range(size):
            for px in range(size):
                row, col = n - 1 - int(py * n / size), int(px * n / size)
                z = grid["heights"][row][col] if 0 <= row < n else None
                if z is not None:
                    v = int(60 + 120 * (z - lo) / ((hi - lo) or 1))
                    canvas.put(px, py, (v - 20, v, v - 30))
    geometry = sorted((a for a in data["actors"] if a[2] == "geometry"), key=lambda a: a[5])
    tops = [a[5] for a in geometry] or [0]
    lo, hi = min(tops), max(tops)
    for label, cls, kind, x, y, top, hx, hy in geometry:
        x0, y1 = to_px(x - hx, y - hy)
        x1, y0 = to_px(x + hx, y + hy)
        v = int(110 + 110 * (top - lo) / ((hi - lo) or 1))
        canvas.rect(x0, y0, max(x0, x1), max(y0, y1), (v, v, v), (v - 50, v - 50, v - 50))
    markers = [a for a in data["actors"] if a[2] == "marker"]
    classes = sorted({a[1] for a in markers}, key=lambda c: -sum(1 for a in markers if a[1] == c))
    colour_of = {c: MAP_COLOURS[i % len(MAP_COLOURS)] for i, c in enumerate(classes)}
    numbered = []
    for i, (label, cls, kind, x, y, top, hx, hy) in enumerate(sorted(markers, key=lambda a: (-a[4], a[3]))):
        px, py = to_px(x, y)
        canvas.dot(px, py, colour_of[cls][0])
        if numbers:
            canvas.number(px + 6, py - 4, str(i + 1))
            numbered.append((i + 1, label, cls, x, y))
    legend = [(colour_of[c][1], c, sum(1 for a in markers if a[1] == c)) for c in classes]
    return canvas, legend, numbered


MAP_STATE = ("import json\nU = unreal.EditorLoadingAndSavingUtils\n"
             "json.dumps({'maps': [p.get_name() for p in U.get_dirty_map_packages()], "
             "'assets': len(U.get_dirty_content_packages())})")


def map_open(ctx, args):
    """`ue map open <path> [--save|--discard]`: load another map into the running editor. Editor scripting loads it
    without asking, so a map with unsaved edits would lose them: refused unless --save or --discard."""
    if not args.path:
        raise CliError("ue map open /Game/Maps/MyMap [--save|--discard]")
    if (ctx.beacon or {}).get("pie"):
        raise CliError("PIE is running (ue pie stop first)")
    state = py_json(ctx, MAP_STATE)
    if state["maps"] and not (args.save or args.discard):
        raise CliError(f"unsaved changes in {', '.join(state['maps'])}: pass --save or --discard (ue changes lists "
                       "them, ue diff <asset> shows what changed)")
    if state["maps"] and args.save:
        ctx.tether("py", code="unreal.EditorLoadingAndSavingUtils.save_dirty_packages(True, False)")
        print(f"saved {', '.join(state['maps'])}")
    target = args.path.split(".")[0]
    if not str(ctx.tether("py", code=f"unreal.EditorAssetLibrary.does_asset_exist({target!r})")).strip().endswith("True"):
        raise CliError(f"no map at {target} (ue find {target.rsplit('/', 1)[-1]})")
    loaded = str(ctx.tether("py", code=f"unreal.get_editor_subsystem(unreal.LevelEditorSubsystem).load_level({target!r})\n"
                                        "unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_editor_world()"
                                        ".get_path_name().split('.')[0]")).strip().strip("'")
    if loaded != target:  # load_level answers True even when nothing was loaded
        raise CliError(f"could not load {target}: the editor is on {loaded} (ue log)")
    print(f"opened {target}" + (f" (unsaved edits to {', '.join(state['maps'])} dropped)" if state["maps"] and args.discard
                                 else "")
          + (f"; {state['assets']} unsaved asset(s) stay unsaved in memory" if state["assets"] else ""))
    return 0


def map_footprints(ctx, args):
    """`ue map --footprints`: the level's collidable geometry from above as data, for a minimap or a layout check:
    each mesh's outline (its own box turned with it), top and bottom, written as JSON."""
    data = ctx.tether("map_data", area=args.area or "-", pie=args.pie, footprints=True)
    data = json.loads(data) if isinstance(data, str) else data
    rx = re.compile(args.match, re.I) if args.match else None
    rows = [{"label": a[0], "class": a[1], "centre": [a[3], a[4]], "half": [a[6], a[7]], "top": a[5], "bottom": a[8],
             "outline": a[9]} for a in data["actors"] if a[2] == "geometry" and len(a) > 9
            and (not rx or rx.search(a[0]) or rx.search(a[1]))]
    if not rows and data["actors"] and not any(len(a) > 9 for a in data["actors"]):
        raise CliError("this editor's Tether plugin predates --footprints (ue init, then ue reload)")
    path = os.path.abspath(args.output) if args.output and args.output.endswith(".json") else os.path.join(
        state_dir(ctx.project), "map", time.strftime("footprints-%Y%m%d-%H%M%S.json"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"footprints": rows, "landscape": data["landscape"]}, f)
    ctx.data = {"path": path, "footprints": rows}
    if rows:
        xs = [p[0] for r in rows for p in r["outline"]]
        ys = [p[1] for r in rows for p in r["outline"]]
        zs = [r["top"] for r in rows] + [r["bottom"] for r in rows]
        print(f"{len(rows)} footprint(s) over x {min(xs):.0f}..{max(xs):.0f}, y {min(ys):.0f}..{max(ys):.0f}, "
              f"z {min(zs):.0f}..{max(zs):.0f}: {path}")
    else:
        print(f"no collidable geometry found ({path})")
    print("  each: label, class, outline (4 x,y corners: the mesh's box turned with it), top, bottom, centre, half; "
          "--json puts them in the output too")
    return 0


def cmd_map(ctx, args):
    """`ue map`: the level (or an area) from above as a picture, with gameplay actors marked by class."""
    import math
    if args.area == "open":
        return map_open(ctx, args)
    if args.footprints:
        return map_footprints(ctx, args)
    data = ctx.tether("map_data", area=args.area or "-", pie=args.pie)
    data = json.loads(data) if isinstance(data, str) else data
    if args.match:
        rx = re.compile(args.match, re.I)
        data["actors"] = [a for a in data["actors"] if a[2] == "geometry" or rx.search(a[0]) or rx.search(a[1])]
    actors = data["actors"]
    if args.area:
        nums = [float(v) for v in args.area.split(",")]
        centre, half = (nums[0], nums[1]), nums[2]
    else:
        if not actors and not data["landscape"]:
            raise CliError("nothing to draw: the level has no meshes or gameplay actors")
        xs = [a[3] - a[6] for a in actors] + [a[3] + a[6] for a in actors]
        ys = [a[4] - a[7] for a in actors] + [a[4] + a[7] for a in actors]
        if data["landscape"]:
            try:
                info = ctx.tether("landscape", action="grid", x=0.0, y=0.0, size=0.0, px=2)
                info = json.loads(info) if isinstance(info, str) else info
                xs += [info["x"] - info["size"] / 2, info["x"] + info["size"] / 2]
                ys += [info["y"] - info["size"] / 2, info["y"] + info["size"] / 2]
            except (CliError, KeyError, TypeError):
                pass
        centre = ((min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2)
        half = max(max(xs) - min(xs), max(ys) - min(ys)) / 2 * 1.05 + 100
    grid = None
    if data["landscape"]:
        try:
            grid = ctx.tether("landscape", action="grid", x=centre[0], y=centre[1], size=half * 2, px=128)
            grid = json.loads(grid) if isinstance(grid, str) else grid
            grid = grid if isinstance(grid, dict) and grid.get("heights") else None
        except CliError:
            grid = None
    markers = [a for a in actors if a[2] == "marker"]
    canvas, legend, numbered = map_picture(data, args.px, centre, half, grid, numbers=len(markers) <= args.numbers)
    path = args.output or os.path.join(state_dir(ctx.project), "shots", time.strftime("map-%Y%m%d-%H%M%S.png"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    png_write(path, args.px, args.px, bytes(canvas.px))
    geometry = len(actors) - len(markers)
    print(f"{path}  {args.px}x{args.px}, {half * 2 / 100:.0f} m across centred on ({centre[0]:.0f}, {centre[1]:.0f}); "
          f"x right, y up; 1 px = {half * 2 / args.px:.0f} cm")
    print(f"{geometry} mesh footprint(s) in grey (lighter = taller)" + (", landscape height underneath" if grid else "")
          + f"; {len(markers)} marker(s):")
    for colour, cls, count in legend:
        print(f"  {colour:<9} {cls} x{count}")
    for n, label, cls, x, y in numbered[: args.limit]:
        print(f"  {n:>3} {label} ({cls}) at {x:.0f}, {y:.0f}")
    if markers and not numbered:
        print(f"  (markers not numbered: more than {args.numbers}; --match narrows them)")
    ctx.data = {"path": path, "centre": centre, "half": half, "legend": legend, "numbered": numbered}
    return 0
