"""Local metadata store.

Keeps, per game (keyed by normalised name):
  * the SteamGridDB id that was matched - so a search only happens once
  * every appid the game has ever had - so artwork can be re-linked when
    Faugus/Heroic rewrite shortcuts.vdf and the CRC-derived appid changes
  * which image was applied to each slot

This is what BoilR lacks, and the reason its users end up with orphaned art.
"""

import json
import os
import time
from pathlib import Path

from .sgdb import normalize_name

VERSION = 1
MAX_APPIDS = 8


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.data = {"version": VERSION, "games": {}}
        self.load()

    def load(self):
        if self.path.is_file():
            try:
                loaded = json.loads(self.path.read_text())
                if isinstance(loaded, dict) and isinstance(loaded.get("games"), dict):
                    self.data = loaded
            except (OSError, ValueError):
                pass
        return self.data

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.data, indent=1, ensure_ascii=False))
        os.replace(tmp, self.path)

    # -- entries ---------------------------------------------------------
    def entry(self, name, create=True):
        key = normalize_name(name)
        if not key:
            return None
        games = self.data["games"]
        if key not in games:
            if not create:
                return None
            games[key] = {"name": name, "sgdb_id": None, "appids": [], "art": {}, "updated": 0}
        return games[key]

    def by_appid(self, appid):
        appid = str(appid)
        for entry in self.data["games"].values():
            if appid in entry.get("appids", []):
                return entry
        return None

    def set_sgdb(self, name, sgdb_id, sgdb_name="", save=True):
        entry = self.entry(name)
        entry["sgdb_id"] = int(sgdb_id) if sgdb_id else None
        entry["sgdb_name"] = sgdb_name or entry.get("sgdb_name") or ""
        entry["updated"] = time.time()
        if save:
            self.save()
        return entry

    def set_field(self, name, key, value, save=True):
        entry = self.entry(name)
        entry[key] = value
        entry["updated"] = time.time()
        if save:
            self.save()
        return entry

    def remember_appid(self, name, appid, save=True):
        entry = self.entry(name)
        appid = str(appid)
        history = entry.setdefault("appids", [])
        if appid in history:
            history.remove(appid)
        history.insert(0, appid)
        del history[MAX_APPIDS:]
        if save:
            self.save()
        return entry

    def record_art(self, name, slot, image=None, files=None, save=True):
        entry = self.entry(name)
        art = entry.setdefault("art", {})
        if image is None and files is None:
            art.pop(slot, None)
        else:
            art[slot] = {
                "image_id": (image or {}).get("id"),
                "source": (image or {}).get("source", "steamgriddb"),
                "url": (image or {}).get("url", ""),
                "author": (image or {}).get("author", ""),
                "style": (image or {}).get("style", ""),
                "applied": time.time(),
                "files": files or [],
            }
        entry["updated"] = time.time()
        if save:
            self.save()
        return entry

    def forget_appid(self, appid, save=True):
        appid = str(appid)
        for entry in self.data["games"].values():
            if appid in entry.get("appids", []):
                entry["appids"].remove(appid)
        if save:
            self.save()

    # -- imports ---------------------------------------------------------
    def import_boilr_cache(self, path):
        """BoilR stores {appid: [name, steamgriddb_id]} - a useful seed."""
        path = Path(path)
        if not path.is_file():
            return 0
        try:
            cache = json.loads(path.read_text())
        except (OSError, ValueError):
            return 0
        imported = 0
        for appid, value in cache.items():
            if not isinstance(value, (list, tuple)) or len(value) < 2:
                continue
            name, sgdb_id = value[0], value[1]
            if not name:
                continue
            entry = self.entry(str(name).strip())
            if not entry:
                continue
            if sgdb_id and not entry.get("sgdb_id"):
                entry["sgdb_id"] = int(sgdb_id)
                entry["sgdb_name"] = entry.get("sgdb_name") or str(name).strip()
                imported += 1
            self.remember_appid(str(name).strip(), str(appid), save=False)
        self.save()
        return imported
