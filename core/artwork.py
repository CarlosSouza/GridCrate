"""Steam grid artwork: scanning, applying, orphan detection and thumbnails.

Steam reads these files from <userdata>/<account>/config/grid/:

    <appid>p.<ext>            portrait grid   600x900
    <appid>.<ext>             wide grid       920x430   (also mirrored to _bigpicture)
    <appid>_hero.<ext>        hero banner     1920x620
    <appid>_logo.<ext>        transparent logo
    <appid>-icon.<ext>        small icon (png or ico)

An "orphan" is a group of files whose appid no longer belongs to any shortcut -
this happens whenever Faugus/Heroic rewrite shortcuts.vdf with a slightly
different Exe/AppName, since the appid is a CRC of those two fields.
"""

import hashlib
import os
import re
import shutil
import time
from pathlib import Path

from PIL import Image, UnidentifiedImageError

IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".ico")

# Guards: appids come from the UI/API and end up in paths and glob patterns.
APPID_RE = re.compile(r"^\d{1,20}$")


def valid_appid(value):
    """Steam grid appids are unsigned 32-bit decimal numbers."""
    return bool(APPID_RE.match(str(value or "")))


def require_appid(value):
    if not valid_appid(value):
        raise ValueError(f"invalid appid: {value!r}")
    return str(value)


# Anything bigger than this is treated as a decompression bomb rather than art.
Image.MAX_IMAGE_PIXELS = 64_000_000
MAX_SIDE = 12000

SLOTS = {
    "portrait": {
        "suffix": "p",
        "label": "Portrait",
        "hint": "600x900 - library card",
        "kind": "grids",
        "dimensions": "600x900",
        "alts": [],
        "exts": (".png", ".jpg", ".jpeg", ".webp"),
    },
    "wide": {
        "suffix": "",
        "label": "Wide",
        "hint": "920x430 - Big Picture / recent games",
        "kind": "grids",
        "dimensions": "920x430",
        "alts": ["460x215"],
        "exts": (".png", ".jpg", ".jpeg", ".webp"),
    },
    "hero": {
        "suffix": "_hero",
        "label": "Hero",
        "hint": "1920x620 - game page banner",
        "kind": "heroes",
        "dimensions": None,
        "alts": [],
        "exts": (".png", ".jpg", ".jpeg", ".webp"),
    },
    "logo": {
        "suffix": "_logo",
        "label": "Logo",
        "hint": "transparent logo for the game page",
        "kind": "logos",
        "dimensions": None,
        "alts": [],
        "exts": (".png", ".jpg", ".jpeg", ".webp"),
    },
    "icon": {
        "suffix": "-icon",
        "label": "Icon",
        "hint": "square icon shown next to the name",
        "kind": "icons",
        "dimensions": None,
        "alts": [],
        "exts": (".png", ".jpg", ".jpeg", ".webp", ".ico"),
    },
}

SLOT_ORDER = ["portrait", "wide", "hero", "logo", "icon"]

_MIME_EXT = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/webp": ".webp",
    "image/x-icon": ".ico",
    "image/vnd.microsoft.icon": ".ico",
    "image/ico": ".ico",
}


def slot_files(grid_dir: Path, appid: str, slot: str):
    """Existing files for one slot, in preference order."""
    spec = SLOTS[slot]
    found = []
    for ext in spec["exts"]:
        candidate = Path(grid_dir) / f"{appid}{spec['suffix']}{ext}"
        if candidate.is_file():
            found.append(candidate)
    if slot == "icon":
        alternate = Path(grid_dir) / f"{appid}_icon.png"
        if alternate.is_file():
            found.append(alternate)
    return found


_FILE_RE = re.compile(r"^(\d+)(p|_hero|_logo|-icon|_icon|_bigpicture)?\.(\w+)$")
_SUFFIX_SLOT = {"p": "portrait", "": "wide", "_hero": "hero", "_logo": "logo",
                "-icon": "icon", "_icon": "icon", "_bigpicture": None}


def index_grid(grid_dir: Path):
    """One directory pass: {appid: {slot: [files]}} (best file first)."""
    grid_dir = Path(grid_dir)
    index = {}
    if not grid_dir.is_dir():
        return index
    for entry in grid_dir.iterdir():
        if not entry.is_file():
            continue
        match = _FILE_RE.match(entry.name)
        if not match:
            continue
        slot = _SUFFIX_SLOT.get(match.group(2) or "")
        if slot is None:
            continue
        bucket = index.setdefault(match.group(1), {}).setdefault(slot, [])
        if entry.suffix.lower() in SLOTS[slot]["exts"]:
            bucket.append(entry)
    for appid, slots in index.items():
        for slot, files in slots.items():
            order = SLOTS[slot]["exts"]
            canonical = f"{appid}{SLOTS[slot]['suffix']}."
            files.sort(
                key=lambda path: (
                    order.index(path.suffix.lower()) if path.suffix.lower() in order else len(order),
                    0 if path.name.startswith(canonical) else 1,
                )
            )
    return index


def slots_from_index(index, appid, with_sizes=False):
    result = {}
    for slot, files in (index.get(str(appid)) or {}).items():
        path = files[0]
        try:
            stat = path.stat()
        except OSError:
            continue
        info = {"file": str(path), "ext": path.suffix, "size": stat.st_size,
                "count": len(files), "mtime": stat.st_mtime}
        if with_sizes:
            try:
                with Image.open(path) as image:
                    info["width"], info["height"] = image.size
            except (UnidentifiedImageError, OSError):
                info["width"] = info["height"] = 0
        result[slot] = info
    return result


def scan_game(grid_dir: Path, appid: str, with_sizes=True):
    return slots_from_index(index_grid(grid_dir), appid, with_sizes=with_sizes)


def find_orphans(grid_dir: Path, known_appids):
    grid_dir = Path(grid_dir)
    known = set(map(str, known_appids))
    groups = {}
    if not grid_dir.is_dir():
        return groups
    for entry in sorted(grid_dir.iterdir()):
        if not entry.is_file():
            continue
        match = re.match(r"^(\d+)(p|_hero|_logo|-icon|_icon|_bigpicture)?\.(\w+)$", entry.name)
        if not match:
            continue
        appid = match.group(1)
        if appid in known:
            continue
        group = groups.setdefault(
            appid, {"appid": appid, "files": [], "size": 0, "slots": {}, "sample": None}
        )
        group["files"].append(entry.name)
        try:
            group["size"] += entry.stat().st_size
        except OSError:
            pass
        suffix = match.group(2) or ""
        slot = {v["suffix"]: k for k, v in SLOTS.items()}.get(suffix)
        if slot:
            group["slots"].setdefault(slot, entry.name)
        if group["sample"] is None and suffix in ("p", ""):
            group["sample"] = str(entry)
    for group in groups.values():
        if group["sample"] is None:
            for slot in ("hero", "logo", "icon"):
                name = group["slots"].get(slot)
                if name:
                    group["sample"] = str(grid_dir / name)
                    break
    return groups


def _extension_for(data: bytes, mime: str, slot: str, url: str):
    ext = _MIME_EXT.get((mime or "").split(";")[0].strip().lower())
    if not ext and url:
        ext = Path(url.split("?")[0]).suffix.lower()
        ext = ext if ext in IMAGE_EXTS else None
    if not ext:
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            ext = ".png"
        elif data[:3] == b"\xff\xd8\xff":
            ext = ".jpg"
        elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            ext = ".webp"
        else:
            ext = ".png"
    if slot == "icon" and ext in (".webp",):
        ext = ".png"
    return ext


def trash_files(paths, trash_root: Path):
    """Move files aside instead of deleting, so mistakes are recoverable."""
    if not paths:
        return []
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target_dir = Path(trash_root) / stamp
    target_dir.mkdir(parents=True, exist_ok=True)
    moved = []
    for path in paths:
        path = Path(path)
        if not path.exists():
            continue
        destination = target_dir / path.name
        counter = 1
        while destination.exists():
            destination = target_dir / f"{path.stem}.{counter}{path.suffix}"
            counter += 1
        shutil.move(str(path), str(destination))
        moved.append(str(destination))
    return moved


def apply_image(grid_dir, appid, slot, data, url="", mime="", trash_root=None):
    """Write downloaded bytes as the artwork for one slot. Returns written paths."""
    if slot not in SLOTS:
        raise ValueError(f"unknown slot {slot}")
    appid = require_appid(appid)
    check_image_sane(data)
    grid_dir = Path(grid_dir)
    grid_dir.mkdir(parents=True, exist_ok=True)
    ext = _extension_for(data, mime, slot, url)

    if ext == ".webp":
        data, ext = _webp_to_png(data), ".png"
    if ext == ".ico" and slot != "icon":
        data, ext = _ico_to_png(data), ".png"

    target = grid_dir / f"{appid}{SLOTS[slot]['suffix']}{ext}"
    old = [f for f in slot_files(grid_dir, appid, slot) if f != target]
    if trash_root:
        trash_files(old, trash_root)
    else:
        for path in old:
            try:
                os.remove(path)
            except OSError:
                pass

    tmp = target.with_suffix(target.suffix + ".part")
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, target)

    written = [str(target)]
    if slot == "wide":
        mirror = grid_dir / f"{appid}_bigpicture{ext}"
        shutil.copy2(target, mirror)
        written.append(str(mirror))
    return written


def check_image_sane(data):
    """Reject decompression bombs before Pillow allocates anything."""
    import io

    try:
        with Image.open(io.BytesIO(data)) as probe:
            width, height = probe.size
    except Exception as exc:
        raise ValueError(f"not a readable image: {exc}") from exc
    if width <= 0 or height <= 0:
        raise ValueError("image has no size")
    if width > MAX_SIDE or height > MAX_SIDE or width * height > Image.MAX_IMAGE_PIXELS:
        raise ValueError(f"image is too large ({width}x{height})")
    return width, height


def _webp_to_png(data):
    import io

    with Image.open(io.BytesIO(data)) as image:
        buffer = io.BytesIO()
        image.convert("RGBA").save(buffer, format="PNG")
        return buffer.getvalue()


def _ico_to_png(data):
    import io

    with Image.open(io.BytesIO(data)) as image:
        buffer = io.BytesIO()
        image.convert("RGBA").save(buffer, format="PNG")
        return buffer.getvalue()


def remove_slot(grid_dir, appid, slot, trash_root=None):
    files = slot_files(Path(grid_dir), appid, slot)
    if slot == "wide":
        for ext in SLOTS["wide"]["exts"]:
            mirror = Path(grid_dir) / f"{appid}_bigpicture{ext}"
            if mirror.is_file():
                files.append(mirror)
    if trash_root:
        return trash_files(files, trash_root)
    removed = []
    for path in files:
        try:
            os.remove(path)
            removed.append(str(path))
        except OSError:
            pass
    return removed


def relink(grid_dir, from_appid, to_appid, slots=None, move=False, trash_root=None):
    """Copy (or move) artwork from an orphan appid to a live one."""
    grid_dir = Path(grid_dir)
    from_appid, to_appid = require_appid(from_appid), require_appid(to_appid)
    slots = slots or SLOT_ORDER
    copied = []
    for slot in slots:
        files = slot_files(grid_dir, from_appid, slot)
        if not files:
            continue
        source = files[0]
        target = grid_dir / f"{to_appid}{SLOTS[slot]['suffix']}{source.suffix}"
        if target.exists() and target != source:
            continue
        shutil.copy2(source, target)
        copied.append(str(target))
        if slot == "wide":
            mirror = grid_dir / f"{to_appid}_bigpicture{source.suffix}"
            shutil.copy2(source, mirror)
            copied.append(str(mirror))
    if move:
        remaining = set()
        for slot in slots:
            remaining.update(slot_files(grid_dir, from_appid, slot))
        for ext in SLOTS["wide"]["exts"]:
            mirror = grid_dir / f"{from_appid}_bigpicture{ext}"
            if mirror.is_file():
                remaining.add(mirror)
        if trash_root:
            trash_files(sorted(remaining), trash_root)
        else:
            for path in remaining:
                try:
                    os.remove(path)
                except OSError:
                    pass
    return copied


def thumbnail(path, width, cache_dir):
    """Cached scaled jpg of a local image (keeps the library light)."""
    path = Path(path)
    try:
        stat = path.stat()
    except OSError:
        return None
    key = hashlib.md5(f"{path}:{stat.st_mtime_ns}:{stat.st_size}:{width}".encode()).hexdigest()
    cache_dir = Path(cache_dir)
    cached = cache_dir / f"{key}.jpg"
    if cached.is_file():
        return cached
    cache_dir.mkdir(parents=True, exist_ok=True)
    try:
        with Image.open(path) as image:
            image = image.convert("RGBA")
            height = max(1, round(image.height * width / image.width))
            image = image.resize((width, height), Image.LANCZOS)
            background = Image.new("RGB", image.size, (12, 12, 14))
            background.paste(image, mask=image.split()[-1])
            background.save(cached, format="JPEG", quality=88)
    except (UnidentifiedImageError, OSError):
        return None
    return cached


def image_info(path):
    path = Path(path)
    if not path.is_file():
        return None
    info = {"path": str(path), "size": path.stat().st_size, "mtime": path.stat().st_mtime}
    try:
        with Image.open(path) as image:
            info["width"], info["height"] = image.size
    except (UnidentifiedImageError, OSError):
        info["width"] = info["height"] = 0
    return info
