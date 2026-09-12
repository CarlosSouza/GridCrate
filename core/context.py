"""Shared application state: Steam account, artwork index, database, Faugus."""

import re
import time
from pathlib import Path

from . import artwork, config, faugus, sgdb, steam
from .db import Store


class Context:
    def __init__(self):
        self.config = config.load()
        self.store = Store(config.DB_PATH)
        self.steam_root = None
        self.accounts = []
        self.account = None
        self.shortcuts = None
        self._faugus_games = None
        self._faugus_mtime = None
        self._boilr_cache = None
        self.reload()

    # -- discovery -------------------------------------------------------
    def reload(self):
        self.steam_root = steam.find_steam_root()
        self.accounts = steam.list_accounts(self.steam_root) if self.steam_root else []
        chosen = self.config.get("account_id")
        self.account = next((a for a in self.accounts if a["account_id"] == chosen), None)
        if self.account is None:
            self.account = steam.pick_default_account(self.accounts)
            if self.account:
                self.config["account_id"] = self.account["account_id"]
                config.save(self.config)
        self.shortcuts = (
            steam.ShortcutsFile(self.account["shortcuts_path"]) if self.account else None
        )
        self._faugus_games = None
        self._seed_store()

    def _seed_store(self):
        """First run: borrow BoilR's appid -> name map; then track today's appids."""
        if not self.store.data["games"]:
            self.store.import_boilr_cache(Path.home() / ".config/boilr/cache.json")
        if not self.shortcuts:
            return
        changed = False
        for game in self.shortcuts.games():
            entry = self.store.entry(game["name"])
            if entry and game["appid"] not in entry.get("appids", []):
                self.store.remember_appid(game["name"], game["appid"], save=False)
                changed = True
        if changed:
            self.store.save()

    @property
    def grid_dir(self):
        override = self.config.get("grid_dir_override")
        if override:
            return Path(override)
        if self.account:
            return Path(self.account["grid_dir"])
        return None

    def sgdb_client(self):
        return sgdb.SteamGridDB(self.config.get("api_key", ""))

    # -- faugus ----------------------------------------------------------
    @property
    def faugus_games(self):
        path = faugus.GAMES_JSON
        mtime = path.stat().st_mtime if path.is_file() else None
        if self._faugus_games is None or mtime != self._faugus_mtime:
            self._faugus_games = faugus.load_games()
            self._faugus_mtime = mtime
        return self._faugus_games

    def faugus_match(self, game):
        games = self.faugus_games
        if not games:
            return None
        entry = faugus.find_game(games, gameid=game.get("faugus_id"))
        if entry is None:
            entry = faugus.find_game(games, title=game.get("name"))
        return entry

    # -- games -----------------------------------------------------------
    def games(self):
        if not self.shortcuts:
            return []
        raw = self.shortcuts.games()
        grid = self.grid_dir
        index = artwork.index_grid(grid) if grid else {}
        result = []
        for game in raw:
            entry = self.store.entry(game["name"], create=False)
            slots = artwork.slots_from_index(index, game["appid"]) if grid else {}
            result.append(
                {
                    **game,
                    "slots": slots,
                    "missing": [s for s in artwork.SLOT_ORDER if s not in slots],
                    "sgdb_id": (entry or {}).get("sgdb_id"),
                    "sgdb_name": (entry or {}).get("sgdb_name", ""),
                    "appid_history": (entry or {}).get("appids", []),
                }
            )
        return result

    def game(self, appid):
        for game in self.games():
            if game["appid"] == str(appid):
                return game
        return None

    def remember(self, game):
        """Track the current appid so art can be re-linked if it ever changes."""
        self.store.remember_appid(game["name"], game["appid"])

    # -- artwork ---------------------------------------------------------
    def local_slot_files(self, appid):
        return {
            slot: str(files[0])
            for slot in artwork.SLOT_ORDER
            if (files := artwork.slot_files(self.grid_dir, appid, slot))
        }

    def apply_image(self, game, slot, image, sgdb_id=None, sgdb_name="", also_faugus=None, client=None):
        from . import http

        data, mime = http.download_image(image["url"])
        written = artwork.apply_image(
            self.grid_dir,
            game["appid"],
            slot,
            data,
            url=image.get("url", ""),
            mime=mime or image.get("mime", ""),
            trash_root=config.TRASH_DIR,
        )
        self.store.remember_appid(game["name"], game["appid"], save=False)
        if sgdb_id:
            self.store.set_sgdb(game["name"], sgdb_id, sgdb_name, save=False)
        self.store.record_art(game["name"], slot, image=image, files=written, save=False)
        self.store.save()

        mirror = {}
        if also_faugus is None:
            also_faugus = bool(self.config.get("faugus_integration"))
        if also_faugus:
            entry = self.faugus_match(game)
            if entry:
                slot_file = self.local_slot_files(game["appid"])
                mirror = faugus.apply_art(
                    entry,
                    portrait=slot_file.get("portrait"),
                    hero=slot_file.get("hero"),
                    icon=slot_file.get("icon"),
                )
                if mirror:
                    try:
                        faugus.remember_metadata(
                            entry,
                            sgdb_id=self.store.entry(game["name"]).get("sgdb_id"),
                            cover_path=mirror.get("cover"),
                            running_check=steam.faugus_running,
                        )
                    except RuntimeError:
                        mirror["metadata_error"] = "faugus-running"
        return {"files": written, "faugus": mirror}

    def remove_slot(self, game, slot):
        removed = artwork.remove_slot(self.grid_dir, game["appid"], slot, trash_root=config.TRASH_DIR)
        if removed:
            self.store.record_art(game["name"], slot, image=None)
        return removed

    # -- orphans ---------------------------------------------------------
    def orphans(self, fingerprint=False):
        grid = self.grid_dir
        if not grid or not self.shortcuts:
            return []
        live_games = self.games()
        known = {g["appid"] for g in live_games}
        groups = artwork.find_orphans(grid, known)
        result = []
        for appid, group in sorted(groups.items(), key=lambda kv: -kv[1]["size"]):
            entry = self.store.by_appid(appid)
            guess = entry["name"] if entry else None
            source = "history" if guess else None
            if guess is None:
                guess = self._guess_from_boilr(appid)
                source = "boilr" if guess else None
            result.append({**group, "guess": guess, "guess_source": source, "art_match": None,
                           "duplicate_of": None})
        if fingerprint:
            self._fingerprint_orphans(result, live_games)
        for orphan in result:
            target = self._resolve_target(orphan.get("guess"), live_games)
            orphan.update(
                {
                    "target_appid": (target or {}).get("appid"),
                    "target_name": (target or {}).get("name"),
                    "target_missing": (target or {}).get("missing", []),
                    "auto": bool(target),
                }
            )
        return result

    def _resolve_target(self, guess, live_games):
        if not guess:
            return None
        key = sgdb.normalize_name(guess)
        return next((g for g in live_games if sgdb.normalize_name(g["name"]) == key), None)

    def _fingerprint_orphans(self, orphans, live_games):
        from . import faugus, fingerprint

        hashes = fingerprint.Hashes(config.CACHE_DIR / "hashes.json")
        grid = self.grid_dir
        titles = {g.get("gameid"): g.get("title") for g in self.faugus_games}

        references = []
        for game in live_games:
            for slot, info in game["slots"].items():
                if slot not in fingerprint.USABLE_SLOTS:
                    continue
                value = hashes.get(info["file"])
                if value is not None:
                    references.append({"hash": value, "kind": "live", "name": game["name"],
                                       "appid": game["appid"], "slot": slot})
        for icon in sorted(faugus.ICONS.glob("*.png")):
            value = hashes.get(icon)
            if value is not None:
                references.append({"hash": value, "kind": "faugus",
                                   "name": titles.get(icon.stem, icon.stem), "slot": "icon"})

        probes = {}
        for orphan in orphans:
            for slot, filename in orphan["slots"].items():
                if slot not in fingerprint.USABLE_SLOTS:
                    continue
                value = hashes.get(grid / filename)
                if value is not None:
                    probes.setdefault(orphan["appid"], []).append(
                        {"hash": value, "slot": slot, "kind": "orphan", "appid": orphan["appid"]}
                    )

        for orphan in orphans:
            best = None
            for probe in probes.get(orphan["appid"], []):
                limit = fingerprint.threshold_for(probe["slot"])
                for match in fingerprint.best_match(probe["hash"], references, limit=1):
                    if match["distance"] <= limit and (
                        best is None or match["distance"] < best["distance"]
                    ):
                        best = {**match, "probe_slot": probe["slot"]}
            if best:
                orphan["art_match"] = {
                    "kind": best["kind"],
                    "name": best["name"],
                    "distance": best["distance"],
                    "slot": best["probe_slot"],
                    "matched_slot": best["slot"],
                }
                if not orphan["guess"]:
                    orphan["guess"] = best["name"]
                    orphan["guess_source"] = "artwork" if best["kind"] == "live" else "faugus-icon"
            if best and best["kind"] == "live" and best["distance"] <= 2:
                target = self._resolve_target(best["name"], live_games)
                if target and best["slot"] in target["slots"]:
                    orphan["duplicate_of"] = best["name"]

        self._propagate_guesses(orphans, probes, hashes)
        hashes.save()

    def _propagate_guesses(self, orphans, probes, hashes):
        """Orphans that show the same artwork belong to the same game."""
        from . import fingerprint

        changed = True
        while changed:
            changed = False
            for source in orphans:
                if not source["guess"]:
                    continue
                for target in orphans:
                    if target["guess"] or source["appid"] == target["appid"]:
                        continue
                    for probe in probes.get(source["appid"], []):
                        limit = fingerprint.threshold_for(probe["slot"])
                        for other in probes.get(target["appid"], []):
                            if probe["slot"] != other["slot"]:
                                continue
                            if fingerprint.hamming(probe["hash"], other["hash"]) <= limit:
                                target["guess"] = source["guess"]
                                target["guess_source"] = "artwork-twin"
                                target["twin_of"] = source["appid"]
                                changed = True
                                break
                        if changed:
                            break

    def _guess_from_boilr(self, appid):
        if self._boilr_cache is None:
            import json

            path = Path.home() / ".config/boilr/cache.json"
            try:
                self._boilr_cache = json.loads(path.read_text()) if path.is_file() else {}
            except (OSError, ValueError):
                self._boilr_cache = {}
        value = self._boilr_cache.get(str(appid))
        return value[0] if isinstance(value, list) and value else None

    # -- theme -----------------------------------------------------------
    def accent_color(self):
        """Follow the KDE colour scheme so the UI matches the desktop."""
        kdeglobals = Path.home() / ".config/kdeglobals"
        try:
            text = kdeglobals.read_text(errors="ignore")
        except OSError:
            return None
        match = re.search(r"^ColorScheme=(.+)$", text, re.M)
        if not match:
            return None
        scheme = Path.home() / ".local/share/color-schemes" / f"{match.group(1).strip()}.colors"
        if not scheme.is_file():
            return None
        try:
            section = re.search(
                r"\[Colors:Selection\](.*?)(?=\n\[|\Z)", scheme.read_text(errors="ignore"), re.S
            )
            value = re.search(r"BackgroundNormal=(\d+),(\d+),(\d+)", section.group(1))
        except (OSError, AttributeError):
            return None
        if not value:
            return None
        return "#%02x%02x%02x" % tuple(int(part) for part in value.groups())

    # -- status ----------------------------------------------------------
    def status(self):
        games = self.games()
        return {
            "steam": {
                "root": str(self.steam_root) if self.steam_root else None,
                "running": steam.steam_running(),
                "accounts": self.accounts,
                "account_id": self.account["account_id"] if self.account else None,
                "grid_dir": str(self.grid_dir) if self.grid_dir else None,
            },
            "faugus": {
                "available": faugus.available(),
                "running": steam.faugus_running(),
                "count": len(self.faugus_games),
            },
            "config": self.config,
            "theme": {"accent": self.accent_color()},
            "stats": {
                "games": len(games),
                "complete": sum(1 for g in games if not g["missing"]),
                "missing": sum(1 for g in games if not g["slots"]),
                "partial": sum(1 for g in games if g["slots"] and g["missing"]),
                "orphans": len(self.orphans()),
                "slots": list(artwork.SLOT_ORDER),
            },
            "time": time.time(),
        }
