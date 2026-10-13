"""Content hygiene and intake: ue import."""
import json
import os

from .core import CliError, filter_log, log_end, log_path
from .calls import flush_editor_log

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
