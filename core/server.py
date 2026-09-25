"""HTTP API and static file server.

Everything is bound to 127.0.0.1 and every API call must carry the per-run
token, so a random web page in the user's browser cannot drive the app.
"""

import json
import mimetypes
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from . import artwork, batch, config, faugus, sgdb, sources, steam
from .context import Context

INDEX_PLACEHOLDER = "/*__GRIDCRATE_BOOTSTRAP__*/"

MAX_REQUEST_BYTES = 64 * 1024 * 1024

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": (
        "default-src 'self'; img-src 'self' data: https:; style-src 'self' 'unsafe-inline'; "
        "script-src 'self' 'unsafe-inline'; connect-src 'self'; form-action 'none'; "
        "frame-ancestors 'none'; base-uri 'none'"
    ),
}


class Api:
    def __init__(self, auto_repair=True):
        self.ctx = Context()
        self.token = secrets.token_urlsafe(24)
        self.job = None
        self.repaired = []
        self._lock = threading.Lock()
        if auto_repair and self.ctx.config.get("auto_repair"):
            try:
                self.repaired = batch.auto_repair(self.ctx, confident_only=True)
            except Exception:  # never block startup on this
                self.repaired = []

    # -- helpers ---------------------------------------------------------
    def game(self, appid):
        game = self.ctx.game(appid)
        if game is None:
            raise ApiError(404, f"unknown game {appid}")
        return game

    def start_job(self, starter):
        with self._lock:
            if self.job is not None and self.job.snapshot()["state"] == "running":
                raise ApiError(409, "another job is already running")
            self.job = starter()

    # -- GET -------------------------------------------------------------
    def get(self, path, query):
        if path == "/api/state":
            state = self.ctx.status()
            state["repaired"] = self.repaired
            state["stats"]["repairable"] = len(
                batch.auto_repair(self.ctx, dry_run=True, confident_only=True)
            )
            return state
        if path == "/api/games":
            return {"games": self.ctx.games()}
        if path == "/api/game":
            appid = query["appid"][0]
            game = self.game(appid)
            slots = artwork.scan_game(self.ctx.grid_dir, appid, with_sizes=True)
            entry = self.ctx.store.entry(game["name"], create=False) or {}
            faugus_game = self.ctx.faugus_match(game) or {}
            return {
                "game": {**game, "slots": slots},
                "art": entry.get("art", {}),
                "sgdb_id": entry.get("sgdb_id"),
                "sgdb_name": entry.get("sgdb_name", ""),
                "faugus": {"gameid": faugus_game.get("gameid"), "title": faugus_game.get("title"),
                           "steamgriddb_id": faugus_game.get("steamgriddb_id")},
                "slots_spec": artwork.SLOTS,
                "slot_order": artwork.SLOT_ORDER,
            }
        if path == "/api/search":
            term = query.get("q", [""])[0]
            results = self.ctx.sgdb_client().search(term)
            for item in results:
                item["score"] = sgdb.match_score(term, item["name"])
            results.sort(key=lambda item: -item["score"])
            return {"results": results}
        if path == "/api/sources":
            return {"sources": sources.info(self.ctx),
                    "browse": {"google": "https://www.google.com/search?tbm=isch&q=",
                               "bing": "https://www.bing.com/images/search?q="}}
        if path == "/api/candidates":
            appid = query["appid"][0]
            slot = query["slot"][0]
            source = query.get("source", ["steamgriddb"])[0]
            term = query.get("q", [None])[0]
            game = self.game(appid)
            try:
                result = sources.candidates(self.ctx, source, game, slot, query=term)
            except Exception as exc:  # network hiccups should not blank the panel
                return {"images": [], "source": source, "note": f"{type(exc).__name__}: {exc}"}
            for item in result.get("images", []):
                item.setdefault("game_id", None)
            return result
        if path == "/api/orphans":
            orphans = self.ctx.orphans(fingerprint=True)
            return {
                "orphans": orphans,
                "repair": batch.auto_repair(self.ctx, dry_run=True, orphans=orphans,
                                            confident_only=False),
            }
        if path == "/api/faugus/games":
            games = [
                {"gameid": g.get("gameid"), "title": g.get("title"), "path": g.get("path"),
                 "icon": g.get("icon"), "cover": g.get("cover")}
                for g in self.ctx.faugus_games
                if g.get("gameid") not in ("epic-games", "ubisoft-connect", "ea-games")
            ]
            return {"games": sorted(games, key=lambda g: (g["title"] or "").lower())}
        if path == "/api/job":
            return {"job": self.job.snapshot() if self.job else None}
        if path == "/api/backups":
            backups = []
            if self.ctx.account:
                path = Path(self.ctx.account["shortcuts_path"])
                for candidate in sorted(path.parent.glob(f"{path.name}.bak-*"), reverse=True):
                    backups.append({"name": candidate.name, "size": candidate.stat().st_size,
                                    "mtime": candidate.stat().st_mtime})
            return {"backups": backups[:10]}
        raise ApiError(404, f"no route for GET {path}")

    # -- POST ------------------------------------------------------------
    def post(self, path, payload):
        if path == "/api/apply":
            game = self.game(payload["appid"])
            image = payload["image"]
            is_sgdb = image.get("source", "steamgriddb") == "steamgriddb"
            result = self.ctx.apply_image(
                game, payload["slot"], image,
                sgdb_id=(payload.get("sgdb_id") or image.get("game_id")) if is_sgdb else None,
                sgdb_name=payload.get("sgdb_name", ""),
                also_faugus=payload.get("faugus"),
            )
            return {"ok": True, **result, "game": self.ctx.game(game["appid"])}
        if path == "/api/apply_upload":
            return self._apply_upload(payload)
        if path == "/api/remove":
            game = self.game(payload["appid"])
            removed = self.ctx.remove_slot(game, payload["slot"])
            return {"ok": bool(removed), "removed": removed, "game": self.ctx.game(game["appid"])}
        if path == "/api/match":
            game = self.game(payload["appid"])
            source = payload.get("source", "steamgriddb")
            match_id = payload.get("match_id") or payload.get("sgdb_id")
            name = payload.get("name", "")
            if source == "steam":
                self.ctx.store.set_field(game["name"], "steam_appid", match_id)
                self.ctx.store.set_field(game["name"], "steam_name", name)
            elif source == "igdb":
                self.ctx.store.set_field(game["name"], "igdb_id", match_id)
                self.ctx.store.set_field(game["name"], "igdb_name", name)
            else:
                self.ctx.store.set_sgdb(game["name"], match_id, name)
            entry = self.ctx.store.entry(game["name"], create=False) or {}
            return {"ok": True, "source": source, "match_id": match_id,
                    "name": name or entry.get("sgdb_name", "")}
        if path == "/api/batch/fetch":
            appids = payload.get("appids") or []
            slots = payload.get("slots") or artwork.SLOT_ORDER
            self.start_job(lambda: batch.start_fetch(
                self.ctx, appids, slots,
                only_missing=payload.get("only_missing", True),
                threshold=float(payload.get("threshold", 0.72)),
                faugus=payload.get("faugus"),
                overwrite=payload.get("overwrite", False),
                prefer_official=payload.get("prefer_official", False),
            ))
            return {"ok": True, "job": self.job.snapshot()}
        if path == "/api/batch/cancel":
            if self.job:
                self.job.cancel()
            return {"ok": True}
        if path == "/api/orphans/relink":
            for key in ("from_appid", "to_appid"):
                if not artwork.valid_appid(payload.get(key)):
                    raise ApiError(400, f"invalid appid for {key}")
            live = {game["appid"] for game in self.ctx.games()}
            if payload["from_appid"] in live:
                raise ApiError(409, f"appid {payload['from_appid']} belongs to a live shortcut "
                                    "- the orphans list is stale, refresh the page")
            if payload["to_appid"] not in live:
                raise ApiError(409, "target appid is not a live shortcut")
            files = artwork.relink(
                self.ctx.grid_dir, payload["from_appid"], payload["to_appid"],
                slots=payload.get("slots"), move=payload.get("move", False),
                trash_root=config.TRASH_DIR,
            )
            game = self.ctx.game(payload["to_appid"])
            if game:
                self.ctx.store.remember_appid(game["name"], payload["to_appid"])
            return {"ok": True, "files": files, "game": self.ctx.game(payload["to_appid"])}
        if path == "/api/orphans/delete":
            grid = self.ctx.grid_dir
            if self.ctx.shortcuts is None:
                raise ApiError(409, "cannot verify live shortcuts - refusing to delete")
            live = {game["appid"] for game in self.ctx.games()}
            removed = []
            for appid in payload.get("appids", []):
                if not artwork.valid_appid(appid):
                    raise ApiError(400, f"invalid appid: {appid!r}")
                if appid in live:
                    raise ApiError(409, f"appid {appid} belongs to a live shortcut "
                                        "- the orphans list is stale, refresh the page")
                paths = [p for p in Path(grid).glob(f"{appid}*") if p.is_file()]
                removed.extend(artwork.trash_files(paths, config.TRASH_DIR))
            return {"ok": True, "removed": removed}
        if path == "/api/orphans/repair":
            orphans = self.ctx.orphans(fingerprint=True)
            actions = batch.auto_repair(self.ctx, orphans=orphans, confident_only=False)
            return {"ok": True, "actions": actions,
                    "orphans": self.ctx.orphans(fingerprint=True)}
        if path == "/api/shortcuts/import_faugus":
            return self._import_faugus(payload["gameid"])
        if path == "/api/shortcuts/add":
            appid = self.ctx.shortcuts.add(
                payload["name"].strip(),
                _quote_exe(payload["exe"].strip()),
                payload.get("launch_options", ""),
                payload.get("start_dir", ""),
                payload.get("icon", ""),
            )
            self.ctx.store.remember_appid(payload["name"].strip(), appid)
            return {"ok": True, "appid": appid}
        if path == "/api/shortcuts/update":
            fields = {}
            for key in ("AppName", "Exe", "LaunchOptions", "StartDir", "icon"):
                if key in payload:
                    fields[key] = payload[key]
            if "Exe" in fields:
                fields["Exe"] = _quote_exe(fields["Exe"].strip())
            if "hidden" in payload:
                fields["IsHidden"] = payload["hidden"]
            entry = self.ctx.shortcuts.update(payload["index"], **fields)
            if "AppName" in fields:
                self.ctx.store.remember_appid(fields["AppName"], steam.crc_appid(entry["Exe"], entry["AppName"]))
            return {"ok": True, "game": self.ctx.game(str(steam.crc_appid(entry["Exe"], entry["AppName"])))}
        if path == "/api/shortcuts/remove":
            self.ctx.shortcuts.remove(payload["index"])
            return {"ok": True}
        if path == "/api/config":
            allowed = set(config.DEFAULTS)
            for key, value in payload.items():
                if key in allowed:
                    self.ctx.config[key] = value
            self.ctx.config = config.save(self.ctx.config)
            self.ctx.reload()
            return {"ok": True, "config": self.ctx.config, "state": self.ctx.status()}
        if path == "/api/test-key":
            client = sgdb.SteamGridDB(payload.get("api_key") or self.ctx.config.get("api_key", ""))
            ok, message = client.test_key()
            return {"ok": ok, "message": message}
        if path == "/api/steam/shutdown":
            return {"ok": True, "output": _steam_shutdown()}
        if path == "/api/steam/restart":
            _steam_shutdown()
            time.sleep(1.5)
            subprocess.Popen(
                ["setsid", "steam"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            return {"ok": True}
        if path == "/api/browse":
            term = (payload.get("query") or "").strip()
            engine = payload.get("engine", "google")
            if not term:
                raise ApiError(400, "empty query")
            prefix = {"bing": "https://www.bing.com/images/search?q=",
                      "google": "https://www.google.com/search?tbm=isch&q="}.get(engine)
            if prefix is None:
                raise ApiError(400, "unknown engine")
            from urllib.parse import quote

            subprocess.Popen(["xdg-open", f"{prefix}{quote(term)}"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return {"ok": True}
        if path == "/api/open":
            target = payload.get("target", "grid")
            paths = {
                "grid": str(self.ctx.grid_dir),
                "config": str(config.CONFIG_DIR),
                "trash": str(config.TRASH_DIR),
                "covers": str(faugus.COVERS),
            }
            folder = paths.get(target)
            if not folder:
                raise ApiError(400, "unknown target")
            subprocess.Popen(["xdg-open", folder], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return {"ok": True, "path": folder}
        if path == "/api/rescan":
            self.ctx.reload()
            return {"ok": True, "state": self.ctx.status()}
        raise ApiError(404, f"no route for POST {path}")

    def _apply_upload(self, payload):
        import base64
        import binascii

        game = self.game(payload["appid"])
        slot = payload["slot"]
        raw = payload.get("data", "")
        if "," in raw[:64]:
            raw = raw.split(",", 1)[1]
        try:
            data = base64.b64decode(raw, validate=False)
        except (binascii.Error, ValueError) as exc:
            raise ApiError(400, f"could not decode the image: {exc}")
        if not data:
            raise ApiError(400, "empty upload")
        if len(data) > 40 * 1024 * 1024:
            raise ApiError(400, "image is too large")
        try:
            from PIL import Image
            import io

            with Image.open(io.BytesIO(data)) as probe:
                probe.verify()
        except Exception as exc:
            raise ApiError(400, f"not a readable image: {exc}")
        written = artwork.apply_image(
            self.ctx.grid_dir, game["appid"], slot, data,
            url=payload.get("name", "upload"),
            mime=payload.get("mime", ""),
            trash_root=config.TRASH_DIR,
        )
        self.ctx.store.remember_appid(game["name"], game["appid"], save=False)
        self.ctx.store.record_art(
            game["name"], slot,
            image={"source": "file", "id": payload.get("name", ""), "url": payload.get("name", ""),
                   "author": "local file", "style": "custom"},
            files=written, save=False,
        )
        self.ctx.store.save()
        return {"ok": True, "files": written, "game": self.ctx.game(game["appid"])}

    def _import_faugus(self, gameid):
        if not re.match(r"^[A-Za-z0-9._-]{1,80}$", str(gameid or "")):
            raise ApiError(400, "invalid Faugus game id")
        games = self.ctx.faugus_games
        entry = faugus.find_game(games, gameid=gameid)
        if entry is None:
            raise ApiError(404, f"Faugus game {gameid} not found")
        launcher = shutil.which("faugus-launcher") or "/usr/bin/faugus-launcher"
        exe = f'"{launcher}"'
        name = entry.get("title") or gameid
        icon = entry.get("icon") or str(faugus.ICONS / f"{gameid}.png")
        start_dir = os.path.dirname(os.path.expanduser(entry.get("path", ""))) or ""
        appid = self.ctx.shortcuts.add(name, exe, f"--game {gameid}", start_dir, icon)
        self.ctx.store.remember_appid(name, appid)
        return {"ok": True, "appid": appid, "name": name}


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def _quote_exe(exe):
    if not exe:
        return exe
    if exe.startswith('"') and exe.endswith('"'):
        return exe
    if " " in exe:
        return f'"{exe}"'
    return exe


def _steam_shutdown(timeout=25):
    if not steam.steam_running():
        return "steam is not running"
    subprocess.Popen(["steam", "-shutdown"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not steam.steam_running():
            return "steam closed"
        time.sleep(0.5)
    return "steam is still running"


class Handler(BaseHTTPRequestHandler):
    server_version = "GridCrate/1.0"
    api: Api = None

    def log_message(self, fmt, *args):  # keep the terminal quiet
        pass

    # -- plumbing --------------------------------------------------------
    def _authorised(self, query):
        token = self.headers.get("X-GridCrate-Token") or (query.get("token", [None])[0])
        return token == self.api.token

    def _send(self, status, body, content_type="application/json", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in SECURITY_HEADERS.items():
            self.send_header(key, value)
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _send_file(self, path, cache=False, etag=None):
        path = Path(path)
        if not path.is_file():
            return self._send(404, {"error": "not found"})
        content_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(path.stat().st_size))
        self.send_header("Cache-Control", "public, max-age=604800" if cache else "no-store")
        if etag:
            self.send_header("ETag", etag)
        for key, value in SECURITY_HEADERS.items():
            self.send_header(key, value)
        self.end_headers()
        try:
            with open(path, "rb") as fh:
                shutil.copyfileobj(fh, self.wfile)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        path = unquote(parsed.path)
        try:
            if path == "/" or path == "/index.html":
                return self._index()
            if path == "/api/ping":
                return self._send(200, {"app": "gridcrate", "pid": os.getpid()})
            if path.startswith("/static/"):
                return self._static(path)
            if path.startswith("/api/"):
                if not self._authorised(query):
                    return self._send(403, {"error": "bad token"})
                if path == "/api/thumb":
                    return self._thumb(query)
                if path == "/api/full":
                    return self._full(query)
                return self._send(200, self.api.get(path, query))
            return self._send(404, {"error": "not found"})
        except ApiError as exc:
            return self._send(exc.status, {"error": exc.message})
        except Exception as exc:  # noqa: BLE001 - surface it to the UI
            return self._send(500, {"error": f"{type(exc).__name__}: {exc}"})

    def do_POST(self):
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        path = unquote(parsed.path)
        try:
            if not path.startswith("/api/"):
                return self._send(404, {"error": "not found"})
            if not self._authorised(query):
                return self._send(403, {"error": "bad token"})
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_REQUEST_BYTES:
                return self._send(413, {"error": "request body too large"})
            payload = json.loads(self.rfile.read(length) or b"{}")
            return self._send(200, self.api.post(path, payload))
        except ApiError as exc:
            return self._send(exc.status, {"error": exc.message})
        except RuntimeError as exc:
            return self._send(409, {"error": str(exc)})
        except Exception as exc:  # noqa: BLE001
            return self._send(500, {"error": f"{type(exc).__name__}: {exc}"})

    # -- static ----------------------------------------------------------
    def _index(self):
        index = config.WEB_DIR / "index.html"
        html = index.read_text()
        bootstrap = json.dumps({"token": self.api.token})
        html = html.replace(INDEX_PLACEHOLDER, f"window.__GRIDCRATE__ = {bootstrap};")
        return self._send(200, html.encode(), "text/html; charset=utf-8")

    def _static(self, path):
        name = path[len("/static/"):]
        root = config.WEB_DIR.resolve()
        target = (root / name).resolve()
        if target != root and not target.is_relative_to(root):
            return self._send(403, {"error": "nope"})
        return self._send_file(target)

    # -- media -----------------------------------------------------------
    def _appid(self, query):
        value = query.get("appid", [""])[0]
        if not artwork.valid_appid(value):
            raise ApiError(400, "invalid appid")
        return value

    def _thumb(self, query):
        appid = self._appid(query)
        slot = query["slot"][0]
        width = int(query.get("w", ["320"])[0])
        files = artwork.slot_files(self.api.ctx.grid_dir, appid, slot)
        if not files:
            return self._send(404, {"error": "no artwork"})
        thumb = artwork.thumbnail(files[0], width, config.THUMB_DIR)
        if thumb is None:
            return self._send(404, {"error": "not an image"})
        try:
            stat = files[0].stat()
            etag = f'"{int(stat.st_mtime_ns)}-{stat.st_size}"'
        except OSError:
            etag = None
        return self._send_file(thumb, cache=True, etag=etag)

    def _full(self, query):
        appid = self._appid(query)
        slot = query["slot"][0]
        files = artwork.slot_files(self.api.ctx.grid_dir, appid, slot)
        if not files:
            return self._send(404, {"error": "no artwork"})
        return self._send_file(files[0], cache=False)


def serve(port=0, host="127.0.0.1"):
    api = Api()
    handler = type("BoundHandler", (Handler,), {"api": api})
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.daemon_threads = True
    actual_port = httpd.server_address[1]
    config.write_runtime({"port": actual_port, "token": api.token, "pid": os.getpid(),
                          "started": time.time()})
    thread = threading.Thread(target=httpd.serve_forever, daemon=True, name="gridcrate-http")
    thread.start()
    return api, httpd, actual_port
