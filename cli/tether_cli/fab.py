"""ue fab: Fab content the user already has (in this project, in the launcher's vault, in other local projects),
bringing it into the project, Fab's download settings, and a clean-up report.

Tether never acquires anything: it does not read the user's account, never drives the Fab store page, and refuses
editor input while a Fab tab is open (transport.SLATE_INPUT). `ue fab open` hands a listing to the user."""
import hashlib
import json
import os
import re
import shutil
import time

from .core import CliError, editor_live, human_bytes, read_json, state_dir, write_json
from .assets import folder_packages
from .content import unused_roots
from .fabfiles import (
    FAB_URL, SHARED_FAB, engine_fit, engine_minor, folder_bytes, listing_url, nearby_projects, project_fab,
    vault_packs, vault_root,
)

SETTINGS = {  # ue fab settings key: (property, ini key, allowed values or None)
    "quality": ("preferredQualityTier", "PreferredQualityTier", ["Low", "Medium", "High", "Raw"]),
    "format": ("preferredDefaultFormat", "PreferredDefaultFormat", ["GLTF", "FBX"]),
    "cache": ("cacheDirectoryPath", "CacheDirectoryPath", None),
    "mode": ("experienceMode", "ExperienceMode", ["Standard", "Performance"]),
}
SETTINGS_OBJECT = "/Script/Fab.Default__FabSettings"


def project_name(project):
    return os.path.splitext(os.path.basename(project))[0]


def content_path(project, package_path):
    """Disk folder of a /Game path."""
    return os.path.join(os.path.dirname(project), "Content", *package_path[len("/Game/"):].split("/")) \
        if package_path.startswith("/Game/") else None


def item_folders(item):
    return item["folders"] if item["kind"] == "library" else [item["path"]]


def item_packages(project, item):
    """{package: bytes} of a Fab item, from disk."""
    sizes = {}
    for folder in item_folders(item):
        try:
            sizes.update(folder_packages(project, folder))
        except CliError:
            pass
    return sizes


def unused_packages(ctx):
    """The /Game packages nothing reaches (ue unused), or None without an editor."""
    if not editor_live(ctx.project):
        return None
    data = ctx.tether("unused", path="/Game", roots=json.dumps(unused_roots(ctx.project)))
    return {package for package, _ in data["unused"]}


def matches(text, query):
    if not query:
        return True
    try:
        return bool(re.search(query, text, re.I))
    except re.error:
        return query.lower() in text.lower()


def short_title(title):
    """A listing title without the keyword list sellers append: 'Modular Town ( Medieval Town Medieval )'."""
    return re.split(r"\s+[(\[]\s", title, maxsplit=1)[0].strip() or title


def item_name(item):
    return f"{item['title']} [{item['id']}]" if item.get("kind") == "megascan" else short_title(item["title"])


def engines_text(engines):
    if not engines:
        return "UE ?"
    return f"UE {engines[0]}" + (f"-{engines[-1]}" if len(engines) > 1 else "")


def resolve_project(path):
    """A .uproject from a project folder or file."""
    path = os.path.abspath(path)
    if os.path.isdir(path):
        found = [f for f in os.listdir(path) if f.endswith(".uproject")]
        if not found:
            raise CliError(f"no .uproject in {path}")
        path = os.path.join(path, found[0])
    if not path.endswith(".uproject") or not os.path.isfile(path):
        raise CliError(f"not an Unreal project: {path}")
    return path


def cmd_fab(ctx, args):
    """Fab content the user has, and getting it into the project; never buys or claims anything."""
    action = args.action or "list"
    handler = {"list": fab_list, "library": fab_library, "add": fab_add, "open": fab_open, "settings": fab_settings,
               "clean": fab_clean}.get(action)
    if not handler:
        raise CliError(f"ue fab {action}: unknown (list, library, add, open, settings, clean)")
    return handler(ctx, args)


# ---------------------------------------------------------------- ue fab (list)

def fab_rows(ctx, items, unused):
    rows = []
    for item in items:
        sizes = item_packages(ctx.project, item)
        idle = [p for p in sizes if unused is not None and p in unused]
        rows.append(dict(item, bytes=sum(sizes.values()), assets=len(sizes), unused=len(idle) if unused is not None else None,
                         unused_bytes=sum(sizes[p] for p in idle)))
    return rows


def fab_list(ctx, args):
    query = " ".join(args.rest) or None
    packs = vault_packs()
    items = [i for i in project_fab(ctx.project, packs) if matches(f"{i['title']} {i['path']}", query)]
    if not items:
        print(f"no Fab content in {project_name(ctx.project)}" + (f" matching '{query}'" if query else "")
              + " (ue fab library: what you have elsewhere)")
        ctx.data = []
        return 0
    unused = None if args.no_use else unused_packages(ctx)
    rows = fab_rows(ctx, items, unused)
    rows.sort(key=lambda r: -r["bytes"])
    mega = [r for r in rows if r["kind"] == "megascan"]
    packs_in = [r for r in rows if r["kind"] == "pack"]
    parts = ([f"{len(mega)} Megascans"] if mega else []) + ([f"{len(packs_in)} pack(s)"] if packs_in else []) + \
        (["the shared Fab materials"] if any(r["kind"] == "library" for r in rows) else [])
    print(f"Fab content in {project_name(ctx.project)}: {human_bytes(sum(r['bytes'] for r in rows))} "
          f"({', '.join(parts)})" + ("" if unused is not None else "; ue open to see what the game uses"))
    shown = rows if args.all else rows[: args.top]
    for r in shown:
        use = "" if r["unused"] is None else (f"  {r['unused']}/{r['assets']} unused" if r["unused"] else "  all used")
        kind = r["kind"] if r["kind"] != "megascan" else f"Megascans {r['type']}"
        print(f"  {human_bytes(r['bytes']):>9}  {item_name(r)[:42]:<42}  {r['path']}  ({kind}){use}")
    if len(rows) > len(shown):
        print(f"  ... +{len(rows) - len(shown)} more (--all)")
    if mega:
        by_type = {}
        for r in mega:
            by_type[r["type"]] = by_type.get(r["type"], 0) + 1
        tiers = {}
        for r in mega:
            for t in r.get("tiers") or []:
                tiers[t] = tiers.get(t, 0) + 1
        print(f"Megascans: {', '.join(f'{k} {v}' for k, v in sorted(by_type.items()))}; tiers: "
              + ", ".join(f"{k} {v}" for k, v in sorted(tiers.items())))
    if unused is not None and any(r["unused"] for r in rows):
        print("unused = no map, config, code or cook rule reaches it (ue fab clean; ue unused <folder> for the assets)")
    ctx.data = [{k: v for k, v in r.items()} for r in rows]
    return 0


# ---------------------------------------------------------------- ue fab library

def fab_library(ctx, args):
    query = " ".join(args.rest) or None
    if args.from_:
        other = resolve_project(args.from_)
        items = [i for i in project_fab(other) if matches(f"{i['title']} {i['path']}", query)]
        print(f"Fab content in {project_name(other)} ({os.path.dirname(other)}): {len(items)} item(s)"
              + (f" matching '{query}'" if query else ""))
        for i in items[: None if args.all else args.top]:
            print(f"  {i['title'][:42]:<42}  {i['path']}  ({i['kind'] if i['kind'] != 'megascan' else 'Megascans ' + i['type']})")
        if len(items) > args.top and not args.all:
            print(f"  ... +{len(items) - args.top} more (--all)")
        if items:
            print(f"copy one in: ue fab add <name> --from {os.path.dirname(other)}")
        ctx.data = items
        return 0
    minor = engine_minor(ctx.project)
    content = os.path.join(os.path.dirname(ctx.project), "Content")
    packs = [p for p in vault_packs() if matches(" ".join([p["title"], p["app"]] + p["folders"]), query)]
    root = vault_root()
    data = {"vault": [], "projects": []}
    if root:
        print(f"Downloaded packs (launcher vault {root}): {len(packs)}" + (f" matching '{query}'" if query else ""))
    else:
        print("no Epic Games Launcher vault found (TETHER_FAB_VAULT to point at one)")
    for p in packs:
        here = [f"/Game/{f}" for f in p["folders"] if os.path.isdir(os.path.join(content, f))]
        fit = engine_fit(p["engines"], minor)
        if here:
            state = f"in this project ({', '.join(here)})"
        elif not p["content"]:
            state = "not downloaded (the launcher's Fab library: Download)"
        elif p["code"]:
            state = "a code project: create a project from it in the launcher"
        elif fit == "newer":
            state = f"made for newer engines than {minor}: it will not load here"
        else:
            state = f'ue fab add "{short_title(p["title"])}"'+ (f"  (listed up to {p['engines'][-1]}; loads in {minor})" if fit == "older" else "")
        print(f"  {short_title(p['title'])[:44]:<44} {human_bytes(p['bytes']):>9}  {engines_text(p['engines']):<12} {state}")
        data["vault"].append(dict(p, here=here, fit=fit))
    others = []
    for other in nearby_projects(ctx.project):
        items = project_fab(other)
        hits = [i for i in items if i["kind"] != "library" and matches(f"{i['title']} {i['path']}", query)]
        if hits:
            others.append((other, hits))
    if others:
        print("Fab content in other projects" + (f" matching '{query}'" if query else "") + ":")
        for other, hits in others:
            mega = sum(1 for i in hits if i["kind"] == "megascan")
            summary = ", ".join(x for x in ([f"{mega} Megascans"] if mega else [])
                                + ([f"{len(hits) - mega} pack(s)"] if len(hits) > mega else []))
            names = ", ".join(item_name(i) for i in hits[:4]) + (", ..." if len(hits) > 4 else "") if query else ""
            print(f"  {project_name(other):<20} {summary:<26} {os.path.dirname(other)}" + (f"\n      {names}" if names else ""))
            data["projects"].append({"project": other, "items": hits})
        print("  (ue fab library --from <folder> lists one; ue fab add <name> --from <folder> copies it in)")
    print(f"Your whole Fab library (including what is not downloaded) is in the Fab tab's My Library or "
          f"{FAB_URL}/library; Tether does not read your account.")
    ctx.data = data
    return 0


# ---------------------------------------------------------------- ue fab add

def same_file(a, b):
    if os.path.getsize(a) != os.path.getsize(b):
        return False
    digests = []
    for path in (a, b):
        h = hashlib.sha1()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        digests.append(h.digest())
    return digests[0] == digests[1]


def copy_plan(pairs):
    """[(src file, dst file)] split into (new, identical, different) for source/destination folder pairs."""
    new, same, differ = [], [], []
    for src_root, dst_root in pairs:
        for dirpath, _, files in os.walk(src_root):
            for name in files:
                src = os.path.join(dirpath, name)
                dst = os.path.join(dst_root, os.path.relpath(src, src_root))
                if not os.path.exists(dst):
                    new.append((src, dst))
                elif same_file(src, dst):
                    same.append((src, dst))
                else:
                    differ.append((src, dst))
    return new, same, differ


def pick(candidates, query, describe):
    if not candidates:
        return None
    exact = [c for c in candidates if describe(c).lower() == (query or "").lower()]
    if len(exact) == 1 or len(candidates) == 1:
        return (exact or candidates)[0]
    names = "\n".join("  " + describe(c) for c in candidates[:12])
    raise CliError(f"'{query}' matches {len(candidates)}; be more specific:\n{names}")


def fab_add(ctx, args):
    query = " ".join(args.rest)
    if not query:
        raise CliError("ue fab add <pack or Megascans name> [--from <project folder>] [--dry-run]")
    target_content = os.path.join(os.path.dirname(ctx.project), "Content")
    minor = engine_minor(ctx.project)
    if args.from_:
        source = resolve_project(args.from_)
        if os.path.normcase(source) == os.path.normcase(ctx.project):
            raise CliError("--from names this project")
        source_minor = engine_minor(source)
        if source_minor and minor and engine_fit([source_minor], minor) == "newer":
            raise CliError(f"{project_name(source)} uses UE {source_minor}, newer than this project's {minor}: its assets will not load here")
        items = [i for i in project_fab(source) if matches(f"{i['title']} {i['path']}", query)]
        item = pick(items, query, lambda i: f"{i['title']}  {i['path']}")
        if not item:
            raise CliError(f"nothing matches '{query}' in {project_name(source)}'s Fab content (ue fab library --from {args.from_})")
        folders = list(item_folders(item))
        if item["kind"] == "megascan":  # Megascans material instances use the shared /Game/Fab materials
            folders += [f"/Game/Fab/{d}" for d in SHARED_FAB if os.path.isdir(os.path.join(os.path.dirname(source), "Content", "Fab", d))]
        pairs = [(content_path(source, f), content_path(ctx.project, f)) for f in folders]
        title, listing, origin = item["title"], item.get("listing"), project_name(source)
        record_paths = item_folders(item)
    else:
        packs = [p for p in vault_packs() if matches(" ".join([p["title"], p["app"]] + p["folders"] + [p["listing"] or ""]), query)]
        pack = pick(packs, query, lambda p: p["title"])
        if not pack:
            raise CliError(f"no downloaded pack matches '{query}' (ue fab library; Megascans and packs in another project: --from <folder>)")
        if not pack["content"]:
            raise CliError(f"{pack['title']} is not downloaded: download it from the launcher's Fab library first")
        if pack["code"]:
            raise CliError(f"{pack['title']} is a code project, not content: create a project from it in the launcher")
        fit = engine_fit(pack["engines"], minor)
        if fit == "newer":
            raise CliError(f"{pack['title']} is made for {engines_text(pack['engines'])}; its assets will not load in UE {minor}")
        if fit == "older":
            print(f"note: {short_title(pack['title'])} lists {engines_text(pack['engines'])}; assets from older engines load in {minor} "
                  "(they are upgraded when saved)")
        pairs = [(pack["content"], target_content)]
        title, listing, origin = pack["title"], pack["listing"], "vault"
        record_paths = [f"/Game/{f}" for f in pack["folders"]]
    pairs = [(s, d) for s, d in pairs if s and os.path.isdir(s)]
    new, same, differ = copy_plan(pairs)
    size = sum(os.path.getsize(s) for s, _ in new)
    destinations = record_paths + [f for f in (folders if args.from_ else []) if f not in record_paths]
    print(f"{short_title(title)}: {len(new)} new file(s), {human_bytes(size)}" + (f"; {len(same)} already here" if same else "")
          + (f"; {len(differ)} differ from what is here" if differ else "") + f" -> {', '.join(destinations)}")
    if differ:
        for src, dst in differ[:8]:
            print(f"  differs: {os.path.relpath(dst, os.path.dirname(ctx.project))}")
        if len(differ) > 8:
            print(f"  ... +{len(differ) - 8} more")
        if not args.skip_existing:
            print("nothing copied: files of the same name differ (another version of this pack?). --skip-existing copies "
                  "only the new files and keeps yours")
            ctx.data = {"new": len(new), "same": len(same), "differ": [d for _, d in differ]}
            return 1
    if args.dry_run or not new:
        print("dry run: nothing copied" if args.dry_run else "nothing to copy")
        ctx.data = {"new": len(new), "same": len(same), "differ": [d for _, d in differ], "bytes": size}
        return 0
    started = time.time()
    # A running editor's directory watcher reads files as they appear, and skips a package it catches half-written
    # ("could not be opened during gathering"): copy beside the project first, then move each finished file in
    # (a rename on one drive is atomic), then have the asset registry rescan the folders.
    staging = os.path.join(state_dir(ctx.project), "fab-staging")
    shutil.rmtree(staging, ignore_errors=True)
    staged = []
    for i, (src, dst) in enumerate(new):
        tmp = os.path.join(staging, f"{i}{os.path.splitext(dst)[1]}")
        os.makedirs(staging, exist_ok=True)
        shutil.copy2(src, tmp)
        staged.append((tmp, dst))
    for tmp, dst in staged:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        try:
            os.replace(tmp, dst)
        except OSError:  # another drive: a plain copy (the rescan below still picks it up)
            shutil.move(tmp, dst)
    shutil.rmtree(staging, ignore_errors=True)
    if editor_live(ctx.project):
        scan = destinations if args.from_ else record_paths
        ctx.tether("py", code="unreal.AssetRegistryHelpers.get_asset_registry().scan_paths_synchronous("
                              f"{scan!r}, True)")
    path = os.path.join(state_dir(ctx.project), "fab.json")
    record = read_json(path) or {}
    for folder in record_paths:
        record.setdefault("added", {})[folder] = {"title": title, "listing": listing, "from": origin,
                                                  "files": len(new), "at": time.strftime("%Y-%m-%d %H:%M")}
    write_json(path, record)
    live = " The editor has scanned them;" if editor_live(ctx.project) else ""
    print(f"copied {len(new)} file(s) in {time.time() - started:.0f}s.{live} next: ue find --under {record_paths[0]}, "
          f"ue lint {record_paths[0]}, ue fab")
    ctx.data = {"copied": len(new), "bytes": size, "folders": record_paths}
    return 0


# ---------------------------------------------------------------- ue fab open

def fab_open(ctx, args):
    query = " ".join(args.rest)
    if args.library or not query:
        url, what = f"{FAB_URL}/library", "your Fab library"
    else:
        found = [i for i in project_fab(ctx.project) if i.get("listing") and matches(f"{i['title']} {i['path']}", query)]
        found += [{"title": p["title"], "listing": p["listing"]} for p in vault_packs()
                  if p["listing"] and matches(f"{p['title']} {p['app']}", query)]
        if query.startswith("/Game/"):
            from .fabfiles import fab_origin
            item = fab_origin(ctx.project, query)
            found = [item] if item and item.get("listing") else found
        uids = {f["listing"]: f["title"] for f in found}
        if re.fullmatch(r"[0-9a-fA-F-]{32,36}", query):
            uids = {query: query}
        if len(uids) == 1:
            uid, what = next(iter(uids.items()))
            url = listing_url(uid)
        elif len(uids) > 1:
            print(f"'{query}' matches {len(uids)} listings:")
            for uid, title in list(uids.items())[:12]:
                print(f"  {title[:50]:<50} {listing_url(uid)}")
            ctx.data = [{"title": t, "url": listing_url(u)} for u, t in uids.items()]
            return 0
        else:
            from urllib.parse import quote
            url, what = f"{FAB_URL}/search?q={quote(query)}", f"a Fab search for '{query}'"
    print(f"{what}: {url}")
    print("Hand this to the user: claiming, buying and downloading are theirs to do (Tether never acts on the store).")
    if args.show:
        import webbrowser
        webbrowser.open(url)
    ctx.data = {"url": url}
    return 0


# ---------------------------------------------------------------- ue fab settings

def settings_ini(project):
    from .fabfiles import project_settings_files
    files = project_settings_files(project)
    return files[0] if files else os.path.join(os.path.dirname(project), "Saved", "Config",
                                               "WindowsEditor" if os.name == "nt" else "LinuxEditor",
                                               "EditorPerProjectUserSettings.ini")


def read_settings_ini(path):
    try:
        with open(path, encoding="utf-8-sig", errors="replace") as f:
            text = f.read()
    except OSError:
        return {}
    section = re.search(r"^\[/Script/Fab\.FabSettings\]\s*$(.*?)(?=^\[|\Z)", text, re.M | re.S)
    return dict(re.findall(r"^(\w+)=(.*)$", section.group(1), re.M)) if section else {}


def write_settings_ini(path, values):
    try:
        with open(path, encoding="utf-8-sig", errors="replace") as f:
            text = f.read()
    except OSError:
        text = ""
    section = re.search(r"^\[/Script/Fab\.FabSettings\]\s*$(.*?)(?=^\[|\Z)", text, re.M | re.S)
    body = section.group(1) if section else "\n"
    for key, value in values.items():
        if re.search(rf"^{key}=.*$", body, re.M):
            body = re.sub(rf"^{key}=.*$", lambda _: f"{key}={value}", body, flags=re.M)
        else:
            body = body.rstrip("\n") + f"\n{key}={value}\n"
    if section:
        text = text[:section.start(1)] + body + text[section.end(1):]
    else:
        text = text.rstrip("\n") + "\n\n[/Script/Fab.FabSettings]" + body
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def fab_settings(ctx, args):
    changes = {}
    for item in args.rest:
        key, eq, value = item.partition("=")
        key = key.strip().lower()
        if not eq or key not in SETTINGS:
            raise CliError(f"ue fab settings [{'|'.join(SETTINGS)}=value ...] (got '{item}')")
        allowed = SETTINGS[key][2]
        if allowed:
            value = next((a for a in allowed if a.lower() == value.strip().lower()), None)
            if not value:
                raise CliError(f"{key}: one of {', '.join(allowed)}")
        elif key == "cache":
            value = os.path.abspath(value.strip()).replace("\\", "/")
            if not os.path.isdir(value):
                raise CliError(f"cache: {value} is not a folder (Fab falls back to its default for missing ones)")
        changes[key] = value
    live = editor_live(ctx.project)
    ini = settings_ini(ctx.project)
    if changes:
        if live:  # through the settings object, which saves its own config on every change
            values = {SETTINGS[k][0]: (json.dumps({"path": v}) if k == "cache" else v) for k, v in changes.items()}
            print(ctx.tether("set_props", target=SETTINGS_OBJECT, values=json.dumps(values), pie=False))
        else:
            write_settings_ini(ini, {SETTINGS[k][1]: (f'(Path="{v}")' if k == "cache" else v) for k, v in changes.items()})
            print(f"set {', '.join(f'{k}={v}' for k, v in changes.items())} in {ini}")
    current = {}
    if live:
        text = str(ctx.tether("props", target=SETTINGS_OBJECT, match="^(" + "|".join(s[0] for s in SETTINGS.values()) + ")$"))
        for key, (prop, _, _) in SETTINGS.items():
            m = re.search(rf"^{prop}: [^=]*= (.*)$", text, re.M)
            if m:
                current[key] = m.group(1).strip()
        if "cache" in current:
            m = re.search(r'"path":"([^"]*)"', current["cache"])
            current["cache"] = m.group(1) if m else current["cache"]
    else:
        stored = read_settings_ini(ini)
        defaults = {"quality": "Medium", "format": "GLTF", "mode": "Standard",
                    "cache": os.path.join(os.environ.get("TEMP", "/tmp"), "FabLibrary").replace("\\", "/")}
        for key, (_, ini_key, _) in SETTINGS.items():
            value = stored.get(ini_key)
            if key == "cache" and value:
                m = re.search(r'Path="([^"]*)"', value)
                value = m.group(1) if m else value
            current[key] = value or defaults[key]
    cache = current.get("cache")
    cache_size = human_bytes(folder_bytes(cache)) if cache and os.path.isdir(cache) else "empty"
    print(f"Fab downloads: Megascans quality {current.get('quality')}, model format {current.get('format')}, "
          f"browser mode {current.get('mode')}")
    print(f"  download cache {cache} ({cache_size}; the Fab plugin clears it after importing)")
    print("  change: ue fab settings quality=High format=FBX cache=D:/FabCache")
    ctx.data = current
    return 0


# ---------------------------------------------------------------- ue fab clean

def fab_clean(ctx, args):
    packs = vault_packs()
    items = project_fab(ctx.project, packs)
    unused = unused_packages(ctx)
    rows = fab_rows(ctx, items, unused)
    findings = 0
    print(f"Fab clean-up report for {project_name(ctx.project)} (report only; nothing is deleted)")
    if unused is None:
        print("  (no editor: what the game uses needs one; ue open, then rerun)")
    else:
        idle = sorted((r for r in rows if r["assets"] and r["unused"] == r["assets"] and r["kind"] != "library"),
                      key=lambda r: -r["bytes"])
        part = sorted((r for r in rows if r["unused"] and r["unused"] < r["assets"]), key=lambda r: -r["unused_bytes"])
        if idle:
            findings += len(idle)
            print(f"nothing uses {len(idle)} Fab item(s), {human_bytes(sum(r['bytes'] for r in idle))}:")
            for r in idle[: args.top]:
                print(f"  {human_bytes(r['bytes']):>9}  {r['title'][:42]:<42}  {r['path']}")
            if len(idle) > args.top:
                print(f"  ... +{len(idle) - args.top} more")
        if part:
            print(f"partly unused ({human_bytes(sum(r['unused_bytes'] for r in part))} in assets nothing reaches):")
            for r in part[: args.top]:
                print(f"  {human_bytes(r['unused_bytes']):>9}  {r['unused']}/{r['assets']}  {r['title'][:42]}  {r['path']}")
        if idle or part:
            print("  check with ue usages <asset> (paths built at runtime look unused), then ue rm or ue unused <folder>")
    tiers = [r for r in rows if r["kind"] == "megascan" and len(r.get("tiers") or []) > 1]
    if tiers:
        findings += len(tiers)
        print(f"{len(tiers)} Megascans asset(s) imported at more than one quality tier (usually one is unused):")
        for r in tiers[: args.top]:
            print(f"  {r['title'][:42]:<42}  {', '.join(r['tiers'])}  {r['path']}")
    content = os.path.join(os.path.dirname(ctx.project), "Content")
    if os.path.isdir(os.path.join(content, "Megascans")) and os.path.isdir(os.path.join(content, "Fab")):
        findings += 1
        print("two Megascans material libraries: Quixel Bridge's /Game/Megascans and Fab's /Game/Fab (assets from "
              "each use their own master materials)")
    dupes = duplicate_files(ctx.project, rows)
    if dupes:
        findings += len(dupes)
        wasted = sum(size * (len(paths) - 1) for size, paths in dupes)
        print(f"{len(dupes)} file(s) stored more than once across Fab folders ({human_bytes(wasted)} extra):")
        for size, paths in sorted(dupes, key=lambda d: -d[0] * (len(d[1]) - 1))[: args.top]:
            print(f"  {human_bytes(size):>9} x{len(paths)}  " + ", ".join(os.path.relpath(p, content) for p in paths[:3]))
    vault = [p for p in packs if p["content"]]
    if vault:
        here = [p for p in vault if any(os.path.isdir(os.path.join(content, f)) for f in p["folders"])]
        print(f"launcher vault: {len(vault)} downloaded pack(s), {human_bytes(sum(p['bytes'] for p in vault))} "
              f"({len(here)} of them in this project; the launcher's Fab library can remove downloads)")
    cache = os.path.join(os.environ.get("TEMP", "/tmp"), "FabLibrary")
    if os.path.isdir(cache):
        print(f"Fab download cache {cache}: {human_bytes(folder_bytes(cache))}")
    if not findings:
        print("nothing to clean up")
    ctx.data = {"rows": rows, "multi_tier": [r["path"] for r in tiers], "duplicates": dupes}
    return 0


def duplicate_files(project, rows):
    """[(bytes, [paths])] of identical package files stored in more than one Fab folder."""
    by_key = {}
    for r in rows:
        for folder in item_folders(r):
            root = content_path(project, folder)
            for dirpath, _, files in os.walk(root or ""):
                for name in files:
                    if name.endswith((".uasset", ".ubulk")):
                        path = os.path.join(dirpath, name)
                        by_key.setdefault((name, os.path.getsize(path)), []).append(path)
    dupes = []
    for (name, size), paths in by_key.items():
        if len(paths) > 1 and size > 4096 and all(same_file(paths[0], p) for p in paths[1:]):
            dupes.append((size, paths))
    return dupes
