"""Fab content from files alone (no editor, no network, no account): the launcher's vault of downloaded packs,
what each project got from Fab, and Fab content in other local projects.

Never reads tokens, cookies or the launcher's web profile: only the vault's manifests and its listings database
(opened read-only), and each project's Content folder and editor settings."""
import glob
import json
import os
import re
import struct

from .core import engine_root, read_json, state_dir, zlib

FAB_URL = "https://www.fab.com"
MEGASCANS = re.compile(r"^/Game/Fab/Megascans/([^/]+)/([^/]+?)_([a-z0-9]{5,10})$")
LISTING_PAIR = re.compile(r'\("(/[^"]+)",\s*"([0-9a-fA-F-]{32,36})"\)')
SHARED_FAB = ("Materials", "MaterialFunctions", "MaterialParameterCollection", "Textures")  # /Game/Fab/<these>


def vault_root():
    """The Epic Games Launcher's vault (downloaded Marketplace and Fab packs), or None."""
    if os.environ.get("TETHER_FAB_VAULT"):
        return os.environ["TETHER_FAB_VAULT"]
    candidates = [os.path.join(os.environ.get("PROGRAMDATA", r"C:\ProgramData"), "Epic", "EpicGamesLauncher", "VaultCache"),
                  os.path.expanduser("~/Library/Application Support/Epic/EpicGamesLauncher/VaultCache"),
                  "/Users/Shared/Epic Games/EpicGamesLauncher/VaultCache"]
    return next((c for c in candidates if os.path.isdir(c)), None)


def engine_minor(project):
    """'5.8' for the project's engine, from Engine/Build/Build.version (None when unknown)."""
    try:
        data = read_json(os.path.join(engine_root(project), "Engine", "Build", "Build.version")) or {}
    except Exception:
        return None
    return f"{data['MajorVersion']}.{data['MinorVersion']}" if "MajorVersion" in data else None


def _fstring(raw, pos):
    n = struct.unpack_from("<i", raw, pos)[0]
    if n < 0:  # UTF-16
        end = pos + 4 - 2 * n
        if n < -(1 << 20) or end > len(raw):
            raise ValueError
        return raw[pos + 4:end].decode("utf-16-le").rstrip("\0"), end
    if n == 0:
        return "", pos + 4
    end = pos + 4 + n
    if n > (1 << 20) or end > len(raw) or raw[end - 1] != 0:
        raise ValueError
    return raw[pos + 4:end - 1].decode("utf-8"), end


def manifest_fields(path):
    """A BuildPatch manifest's custom fields (Vault.TitleText, CompatibleApps, CatalogItemId, ...) and AppName,
    from the JSON form or the binary one (a key array, then the values with no count of their own)."""
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return {}
    if data.lstrip().startswith(b"{"):
        try:
            doc = json.loads(data.decode("utf-8", "replace"))
        except ValueError:
            return {}
        fields = dict(doc.get("CustomFields") or {})
        fields["AppName"] = doc.get("AppNameString", "")
        return fields
    try:
        header, _, compressed = struct.unpack_from("<III", data, 4)
        body = data[header:header + compressed]
        raw = zlib.decompress(body) if data[36] & 1 else body
    except Exception:
        return {}
    anchor = raw.find(b"CompatibleApps\0")
    if anchor < 0:
        return {}
    for start in range(anchor - 5, max(0, anchor - 8192), -1):
        try:
            count = struct.unpack_from("<i", raw, start)[0]
            if not 0 < count <= 256:
                continue
            keys, pos = [], start + 4
            for _ in range(count):
                key, pos = _fstring(raw, pos)
                keys.append(key)
            if "CompatibleApps" not in keys:
                continue
            values = []
            for _ in keys:
                value, pos = _fstring(raw, pos)
                values.append(value)
            return dict(zip(keys, values))
        except (ValueError, UnicodeDecodeError, struct.error):
            continue
    return {}


def engines_of(fields):
    """['5.5', '5.6', ...] from CompatibleApps ('UE_5.5,UE_5.6')."""
    found = re.findall(r"UE_(\d+\.\d+)", fields.get("CompatibleApps", ""))
    return sorted(set(found), key=lambda v: tuple(int(p) for p in v.split(".")))


def engine_fit(engines, minor):
    """'ok', 'older' (made for older engines only: it loads, and is resaved in yours), 'newer' (saved by a newer
    engine: it will not load), or 'unknown'."""
    if not engines or not minor:
        return "unknown"
    def key(version):
        return tuple(int(p) for p in version.split("."))
    if minor in engines:
        return "ok"
    if key(minor) > max(map(key, engines)):
        return "older"
    return "newer" if key(minor) < min(map(key, engines)) else "ok"


def _vault_db(root):
    """{vault folder name: (listing uid, title, cache bytes)} plus [(uid, title)] of every listing, from the
    launcher's Fab listings database (read-only)."""
    path = os.path.join(root, "FabLibrary", "listings_v1.db")
    by_folder, listings = {}, []
    if not os.path.isfile(path):
        return by_folder, listings
    import sqlite3
    db = None
    try:
        # Read-only, and closed straight away: it is the launcher's own database.
        db = sqlite3.connect("file:" + path.replace("\\", "/") + "?mode=ro", uri=True, timeout=2)
        listings = [(uid, title or "") for uid, title in db.execute("SELECT uid, title FROM local_listing")]
        titles = dict(listings)
        for uid, folder, size in db.execute("SELECT listing_uid, path, cache_size FROM download_meta"):
            if folder:
                by_folder[os.path.basename(folder.rstrip("/\\")).lower()] = (uid, titles.get(uid, ""), size or 0)
    except Exception:  # locked or a newer schema: the manifests still tell most of it
        pass
    finally:
        if db is not None:
            db.close()
    return by_folder, listings


def folder_bytes(path):
    total = 0
    for dirpath, _, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(dirpath, name))
            except OSError:
                pass
    return total


def vault_packs(root=None):
    """Packs the launcher downloaded: [{title, app, listing, catalog_item, path, content, folders, engines, bytes}].
    `content` is the folder to copy into a project's Content; `folders` its top-level asset folders."""
    root = root or vault_root()
    if not root or not os.path.isdir(root):
        return []
    by_folder, listings = _vault_db(root)
    packs, seen = [], set()
    for name in sorted(os.listdir(root)):
        folder = os.path.join(root, name)
        manifest = os.path.join(folder, "manifest")
        if not os.path.isfile(manifest):
            continue
        fields = manifest_fields(manifest)
        uid, db_title, size = by_folder.get(name.lower(), (None, "", 0))
        content = os.path.join(folder, "data", "Content")
        title = fields.get("Vault.TitleText") or db_title or name
        if not uid:  # the database may list it without a folder: match by title
            uid = next((u for u, t in listings if t and t == title), None)
        seen.add(uid)
        packs.append({
            "title": title, "app": fields.get("AppName") or name, "listing": uid,
            "catalog_item": fields.get("CatalogItemId"), "path": folder,
            "content": content if os.path.isdir(content) else None,
            "folders": sorted(d for d in os.listdir(content) if not d.startswith("__")) if os.path.isdir(content) else [],
            "engines": engines_of(fields), "bytes": size or (folder_bytes(content) if os.path.isdir(content) else 0),
            "code": fields.get("Vault.IsCodeProject", "").lower() == "true",
        })
    return packs


def project_settings_files(project):
    return glob.glob(os.path.join(os.path.dirname(project), "Saved", "Config", "*Editor", "EditorPerProjectUserSettings.ini"))


def listing_paths(project):
    """{content path: Fab listing uid} the Fab plugin recorded when it imported (its "View in Fab" menu)."""
    found = {}
    for path in project_settings_files(project):
        try:
            with open(path, encoding="utf-8-sig", errors="replace") as f:
                text = f.read()
        except OSError:
            continue
        section = re.search(r"^\[/Script/Fab\.FabLocalAssets\]\s*$(.*?)(?=^\[|\Z)", text, re.M | re.S)
        if section:
            found.update(LISTING_PAIR.findall(section.group(1)))
    record = read_json(os.path.join(state_dir(project), "fab.json")) or {}  # what ue fab add copied in
    for path, info in (record.get("added") or {}).items():
        found.setdefault(path, info.get("listing") or "")
    return found


def _content_dir(project):
    return os.path.join(os.path.dirname(project), "Content")


def megascan_title(name):
    return name.replace("_", " ").strip()


def project_fab(project, packs=None):
    """What a project got from Fab: [{kind, path, title, listing, type, tiers, source}]; kind is megascan, pack
    or library (the shared /Game/Fab materials). From the Fab plugin's own record, the Megascans folder layout,
    vault packs whose folders the project has, and ue fab add's record."""
    content = _content_dir(project)
    listings = listing_paths(project)
    record = (read_json(os.path.join(state_dir(project), "fab.json")) or {}).get("added") or {}
    items = {}
    mega_root = os.path.join(content, "Fab", "Megascans")
    if os.path.isdir(mega_root):
        for kind in sorted(os.listdir(mega_root)):
            kind_dir = os.path.join(mega_root, kind)
            if not os.path.isdir(kind_dir):
                continue
            for name in sorted(os.listdir(kind_dir)):
                path = f"/Game/Fab/Megascans/{kind}/{name}"
                m = MEGASCANS.match(path)
                if not m or not os.path.isdir(os.path.join(kind_dir, name)):
                    continue
                tiers = sorted(d for d in os.listdir(os.path.join(kind_dir, name)) if os.path.isdir(os.path.join(kind_dir, name, d)))
                items[path] = {"kind": "megascan", "path": path, "title": megascan_title(m.group(2)), "type": kind,
                               "id": m.group(3), "listing": listings.get(path), "tiers": tiers}
    if os.path.isdir(os.path.join(content, "Fab")) and any(os.path.isdir(os.path.join(content, "Fab", d)) for d in SHARED_FAB):
        items["/Game/Fab"] = {"kind": "library", "path": "/Game/Fab", "title": "Fab shared materials", "listing": None,
                              "folders": [f"/Game/Fab/{d}" for d in SHARED_FAB if os.path.isdir(os.path.join(content, "Fab", d))]}
    for pack in packs if packs is not None else vault_packs():
        for folder in pack["folders"]:
            path = f"/Game/{folder}"
            if os.path.isdir(os.path.join(content, folder)) and path not in items:
                items[path] = {"kind": "pack", "path": path, "title": pack["title"], "listing": pack["listing"] or listings.get(path),
                               "vault": pack["app"]}
    for path, uid in listings.items():  # anything else the Fab plugin recorded (e.g. a Megaplant library)
        if path not in items and not path.startswith("/Game/Fab/Megascans/"):
            rel = path[len("/Game/"):] if path.startswith("/Game/") else None
            if rel and os.path.isdir(os.path.join(content, *rel.split("/"))):
                items[path] = {"kind": "pack", "path": path, "title": record.get(path, {}).get("title") or rel.split("/")[0].replace("_", " "),
                               "listing": uid}
    for path, info in record.items():
        if path in items:
            items[path].setdefault("source", info.get("from"))
    return sorted(items.values(), key=lambda i: (i["kind"] != "pack", i["path"]))


def fab_origin(project, package, items=None):
    """The Fab item a /Game package came from (the longest matching folder), or None."""
    best = None
    for item in items if items is not None else project_fab(project):
        for folder in [item["path"]] if item["kind"] != "library" else item["folders"]:
            if (package == folder or package.startswith(folder + "/")) and (not best or len(folder) > len(best[0])):
                best = (folder, item)
    return best[1] if best else None


def listing_url(uid):
    return f"{FAB_URL}/listings/{uid}" if uid else None


def nearby_projects(project, limit=40):
    """Other .uproject files near this one (its parent and grandparent folders, two levels down) and in
    TETHER_PROJECTS (os.pathsep-separated folders)."""
    here = os.path.normcase(os.path.abspath(project))
    roots = [os.path.dirname(os.path.dirname(project)), os.path.dirname(os.path.dirname(os.path.dirname(project)))]
    roots += [p for p in os.environ.get("TETHER_PROJECTS", "").split(os.pathsep) if p]
    found, seen = [], set()
    for root in roots:
        if not root or not os.path.isdir(root):
            continue
        for pattern in ("*.uproject", "*/*.uproject", "*/*/*.uproject"):
            for path in glob.glob(os.path.join(root, pattern)):
                key = os.path.normcase(os.path.abspath(path))
                if key != here and key not in seen and "Saved" not in path.split(os.sep) and "Intermediate" not in path.split(os.sep):
                    seen.add(key)
                    found.append(os.path.abspath(path))
    return sorted(found)[:limit]
