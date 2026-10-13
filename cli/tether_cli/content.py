"""Content hygiene and intake: ue import, ue unused, ue lint."""
import fnmatch
import json
import os
import re

from .core import CliError, filter_log, human_bytes, log_end, log_path
from .calls import flush_editor_log
from .assets import SKIP_DIRS, SOURCE_EXTS, package_bytes, project_code_roots

IMPORT_EXTS = {
    "mesh": (".fbx", ".obj", ".glb", ".gltf", ".usd", ".usda", ".usdc", ".usdz", ".abc"),
    "texture": (".png", ".tga", ".jpg", ".jpeg", ".exr", ".hdr", ".bmp", ".tif", ".tiff", ".psd", ".dds"),
    "sound": (".wav", ".ogg", ".flac", ".aif", ".aiff"),
}
IMPORTABLE = tuple(ext for exts in IMPORT_EXTS.values() for ext in exts)


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
        if cls == "Texture2D" and tags.get("Dimensions"):
            w, _, h = tags["Dimensions"].partition("x")
            w, h = int(w or 0), int(h or 0)
            ui = "UI" in (tags.get("LODGroup") or "")
            if not ui and w and h and (w & (w - 1) or h & (h - 1)):
                add("texture-pow2", package, f"{w}x{h} is not a power of two: no mips, no streaming")
            if max(w, h) > max_size:
                add("texture-size", package, f"{w}x{h} is over {max_size}")
            if NORMAL_NAME.search(name) and tags.get("CompressionSettings") not in (None, "TC_Normalmap"):
                add("texture-normal", package, f"named as a normal map but compressed as {tags['CompressionSettings']}")
            if MASK_NAME.search(name) and tags.get("SRGB") == "True":
                add("texture-mask-srgb", package, "a mask/packed texture with sRGB on (its values get gamma-curved)")
        if cls == "StaticMesh" and tags.get("Triangles"):
            tris = int(tags["Triangles"])
            if tags.get("CollisionPrims") == "0" and tags.get("CollisionComplexity") != "CTF_UseComplexAsSimple" \
                    and tags.get("DefaultCollision") != "NoCollision":
                add("mesh-collision", package, f"no simple collision ({tags.get('DefaultCollision') or 'default'} "
                                               "profile): characters and physics pass through it")
            if tags.get("NaniteEnabled") != "True" and tags.get("LODs") == "1" and tris > lod_tris:
                add("mesh-lods", package, f"{tris} triangles, one LOD and no Nanite")
    return found


def cmd_lint(ctx, args):
    """Content rules beyond Epic's validators, from asset registry tags (nothing is loaded)."""
    config_path = os.path.join(os.path.dirname(ctx.project), "Config", "Tether", "lint.json")
    config = {}
    if os.path.isfile(config_path):
        try:
            with open(config_path, encoding="utf-8") as f:
                config = json.load(f)
        except ValueError as exc:
            raise CliError(f"{config_path}: {exc}")
    if args.ignore:
        config["ignore"] = config.get("ignore", []) + args.ignore
    rows = ctx.tether("lint_data", path=args.path)
    rows = json.loads(rows) if isinstance(rows, str) else rows
    found = lint_rules(rows, config)
    if args.rule:
        found = [f for f in found if re.search(args.rule, f[0])]
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
