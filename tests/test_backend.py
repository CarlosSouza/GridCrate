#!/usr/bin/env python3
"""End-to-end tests for GridCrate's backend.

    python3 tests/test_backend.py

Everything runs against a throwaway copy of your real Steam data (sandboxed
through GRIDCRATE_* environment variables), so the live library is never
touched.  If no Steam installation is found the suite skips with a message.
"""

import base64
import io
import json
import os
import re
import shutil
import struct
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zlib
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_DIR))

PASSED, FAILED, SKIPPED = [], [], []
STEAM_CANDIDATES = ("~/.steam/steam", "~/.local/share/Steam",
                    "~/.var/app/com.valvesoftware.Steam/.steam/steam")


def check(name, condition, detail=""):
    (PASSED if condition else FAILED).append(name)
    mark = "ok  " if condition else "FAIL"
    print(f"  {mark} {name}" + (f"  ({detail})" if detail and not condition else ""))


def skip(name, why):
    SKIPPED.append(name)
    print(f"  skip {name}  ({why})")


def find_real_steam():
    override = os.environ.get("GRIDCRATE_STEAM_ROOT")
    for candidate in (override, *STEAM_CANDIDATES):
        if not candidate:
            continue
        path = Path(os.path.expanduser(candidate))
        if (path / "userdata").is_dir():
            return path.resolve()
    return None


def pick_account(root):
    """(account_id, shortcuts_path) of the account with the most shortcuts."""
    best = None
    for entry in sorted((root / "userdata").iterdir()):
        shortcuts = entry / "config" / "shortcuts.vdf"
        if not entry.is_dir() or not entry.name.isdigit() or not shortcuts.is_file():
            continue
        size = shortcuts.stat().st_size
        if best is None or size > best[2]:
            best = (entry.name, shortcuts, size)
    return (best[0], best[1]) if best else (None, None)


def build_sandbox(real_steam, account_id):
    sandbox = Path(tempfile.mkdtemp(prefix="gridcrate-test-"))
    config_dir = sandbox / "userdata" / account_id / "config"
    grid = config_dir / "grid"
    grid.mkdir(parents=True)
    shutil.copy2(real_steam / "userdata" / account_id / "config" / "shortcuts.vdf",
                 config_dir / "shortcuts.vdf")
    # Keep the sandbox small: every file up to 800 KB.
    for item in sorted((real_steam / "userdata" / account_id / "config" / "grid").iterdir()):
        if item.is_file() and item.stat().st_size <= 800_000:
            shutil.copy2(item, grid / item.name)
    (sandbox / "config").mkdir()
    faugus = sandbox / "faugus"
    (faugus / "icons").mkdir(parents=True)
    from PIL import Image

    Image.new("RGBA", (256, 256), (120, 160, 240, 255)).save(faugus / "icons" / "stellar-blade.png")
    (faugus / "games.json").write_text(json.dumps([
        {"gameid": "stellar-blade", "title": "Stellar Blade", "path": "/games/SB.exe",
         "icon": str(faugus / "icons" / "stellar-blade.png")},
    ]))
    os.environ["GRIDCRATE_STEAM_ROOT"] = str(sandbox)
    os.environ["GRIDCRATE_CONFIG_DIR"] = str(sandbox / "config")
    os.environ["GRIDCRATE_CACHE_DIR"] = str(sandbox / "cache")
    os.environ["GRIDCRATE_FAUGUS_BASE"] = str(faugus)
    return sandbox


def guard(sandbox, ctx, label):
    """Hard stop if anything resolves outside the sandbox - these tests write."""
    from core import config, faugus

    sandbox = Path(sandbox).resolve()
    for name, value in (("steam root", ctx.steam_root), ("grid dir", ctx.grid_dir),
                        ("config dir", config.CONFIG_DIR), ("faugus base", faugus.BASE)):
        if value is not None and not Path(value).resolve().is_relative_to(sandbox):
            raise SystemExit(f"REFUSING TO RUN: {label} resolved {name} outside the sandbox: {value}")
    shortcuts = Path(ctx.account["shortcuts_path"]).resolve()
    if not shortcuts.is_relative_to(sandbox):
        raise SystemExit(f"REFUSING TO RUN: shortcuts.vdf outside the sandbox: {shortcuts}")


def main():
    real_steam = find_real_steam()
    if real_steam is None:
        print("No Steam installation found (looked in ~/.steam/steam, ~/.local/share/Steam).")
        print("The integration tests need a Steam userdata folder - skipping.")
        return 0
    account_id, _ = pick_account(real_steam)
    if account_id is None:
        print(f"No account with shortcuts.vdf under {real_steam}/userdata - skipping.")
        return 0

    sandbox = build_sandbox(real_steam, account_id)
    from core import artwork, batch, config, faugus, sgdb, sources as sources_mod, steam, vdfbin
    from core.context import Context
    from core.server import Api

    steam.steam_running = lambda: False  # sandbox: shortcut writes are allowed

    print(f"\n[1] binary VDF codec   (account {account_id})")
    path = Path(os.environ["GRIDCRATE_STEAM_ROOT"]) / "userdata" / account_id / "config" / "shortcuts.vdf"
    original = path.read_bytes()
    parsed = vdfbin.loads(original)
    check("round-trip is byte-identical", vdfbin.dumps(parsed) == original, f"{len(original)} bytes")
    check("all shortcuts parsed", len(parsed["shortcuts"]) > 0, f"{len(parsed['shortcuts'])} entries")

    print("\n[2] appid derivation")
    exe, name = '"/usr/bin/faugus-launcher"', "ROBOBEAT"
    expected = struct.unpack("<i", struct.pack("<I", zlib.crc32((exe + name).encode()) | 0x80000000))[0]
    check("crc32 appid formula", expected == -743685684, f"got {expected}")
    check("signed conversion", steam.to_signed(0x80000000) == -2147483648)
    if parsed["shortcuts"]:
        entry = next(iter(parsed["shortcuts"].values()))
        check("stored appid matches the crc of Exe+AppName",
              entry["appid"] == steam.to_signed(steam.crc_appid(entry["Exe"], entry["AppName"])),
              entry.get("AppName", ""))

    print("\n[3] grid index / orphans")
    ctx = Context()
    guard(sandbox, ctx, "Context")
    index = artwork.index_grid(ctx.grid_dir)
    check("index finds artwork", len(index) >= 1, f"{len(index)} appids with files")
    check("games list built", len(ctx.games()) > 0, f"{len(ctx.games())}")
    check("slot detection finds portrait", any("portrait" in g["slots"] for g in ctx.games()))

    print("\n[4] thumbnails")
    sample = next((g for g in ctx.games() if "portrait" in g["slots"]), None)
    if sample:
        thumb = artwork.thumbnail(Path(sample["slots"]["portrait"]["file"]), 320, config.THUMB_DIR)
        check("thumbnail generated", thumb is not None and thumb.is_file())
        check("thumbnail is smaller than source",
              thumb.stat().st_size < sample["slots"]["portrait"]["size"])
        check("thumbnail cached", artwork.thumbnail(
            Path(sample["slots"]["portrait"]["file"]), 320, config.THUMB_DIR) == thumb)
    else:
        skip("thumbnail tests", "no portrait artwork in the sandbox")

    print("\n[5] SteamGridDB client")
    client = sgdb.SteamGridDB(ctx.config["api_key"])
    if ctx.config.get("api_key"):
        results = client.search("Stellar Blade")
        check("search returns results", bool(results), f"{len(results)} results")
        check("best_match picks the right game",
              (sgdb.best_match("Stellar Blade", results)[0] or {}).get("name") == "Stellar Blade")
        check("match_score rejects nonsense", sgdb.match_score("Stellar Blade", "Hollow Knight") < 0.5)
        images = batch.candidates_for_slot(client, "portrait", 5443884)
        check("portrait candidates", bool(images), f"{len(images)} images")
        check("candidates are static png/jpeg",
              all(image["mime"] in ("image/png", "image/jpeg") for image in images))
        check("hero candidates", bool(batch.candidates_for_slot(client, "hero", 5443884)))
    else:
        skip("SteamGridDB tests", "no API key configured")
        images = []

    print("\n[6] API: apply / remove artwork")
    api = Api()
    guard(sandbox, api.ctx, "Api")
    api.ctx.config["faugus_integration"] = True
    target = next((g for g in api.ctx.games() if "portrait" not in g["slots"]), None)
    if target and images:
        image = images[0]
        response = api.post("/api/apply", {"appid": target["appid"], "slot": "portrait",
                                           "image": image, "sgdb_id": 5443884,
                                           "sgdb_name": "Stellar Blade"})
        written = [Path(p) for p in response["files"]]
        check("apply writes a file", all(p.is_file() for p in written), str(written))
        check("file uses the appid + slot suffix",
              written[0].name.startswith(f"{target['appid']}p."), written[0].name)
        check("game now reports the slot",
              "portrait" in api.ctx.game(target["appid"])["slots"])
        check("db remembers the appid",
              target["appid"] in (api.ctx.store.entry(target["name"]) or {}).get("appids", []))

        from core.db import Store
        on_disk = Store(config.DB_PATH).entry(target["name"], create=False) or {}
        check("db is persisted to disk after apply",
              target["appid"] in on_disk.get("appids", []) and bool(on_disk.get("art", {}).get("portrait")))

        wide = batch.candidates_for_slot(client, "wide", 5443884)[0]
        response = api.post("/api/apply", {"appid": target["appid"], "slot": "wide",
                                           "image": wide, "sgdb_id": 5443884})
        mirror = Path(api.ctx.grid_dir) / f"{target['appid']}_bigpicture{Path(response['files'][0]).suffix}"
        check("wide is mirrored to _bigpicture", mirror.is_file(), str(mirror))

        removal = api.post("/api/remove", {"appid": target["appid"], "slot": "portrait"})
        check("remove moves the file to trash", removal["ok"] and not Path(written[0]).exists())
    else:
        skip("apply/remove tests", "no game missing artwork or no API key")

    print("\n[7] API: batch fetch")
    if target and images:
        job = batch.start_fetch(api.ctx, [target["appid"]], ["portrait", "hero"],
                                only_missing=True, threshold=0.72)
        while job.snapshot()["state"] == "running":
            pass
        snapshot = job.snapshot()
        item = snapshot["items"][0]
        check("batch completed", snapshot["state"] == "done", json.dumps(item)[:200])
        check("batch applied the missing slot", "portrait" in item.get("slots", []),
              json.dumps(item)[:200])
        refreshed = api.ctx.game(target["appid"])
        check("batch filled the requested slots",
              all(slot in refreshed["slots"] for slot in ("portrait", "hero")),
              json.dumps(sorted(refreshed["slots"])))
    else:
        skip("batch fetch", "no game missing artwork or no API key")

    print("\n[8] API: shortcuts CRUD")
    api.post("/api/shortcuts/add", {"name": "GridCrate Test Game", "exe": "/usr/bin/true",
                                    "launch_options": "--test", "start_dir": "/tmp"})
    added = next((g for g in api.ctx.games() if g["name"] == "GridCrate Test Game"), None)
    check("shortcut added", added is not None)
    if added:
        check("appid recomputed correctly",
              added["appid"] == str(steam.crc_appid(added["exe"], "GridCrate Test Game")))
        api.post("/api/shortcuts/update", {"index": added["index"], "AppName": "GridCrate Renamed"})
        check("shortcut renamed", any(g["name"] == "GridCrate Renamed" for g in api.ctx.games()))
        api.post("/api/shortcuts/remove", {"index": added["index"]})
        check("shortcut removed", not any(g["name"] == "GridCrate Renamed" for g in api.ctx.games()))
    backups = sorted(p.name for p in path.parent.glob(path.name + ".bak-*"))
    check("backups were taken", len(backups) >= 1, f"{len(backups)} backups")
    for index in range(14):  # rotation: never keep more than MAX_BACKUPS
        (path.parent / f"{path.name}.bak-20200101-0000{index:02d}").write_bytes(b"x")
    api.ctx.shortcuts._backup()
    kept = sorted(path.parent.glob(path.name + ".bak-*"))
    check("backups rotate", len(kept) <= 10, f"{len(kept)} kept")

    print("\n[9] API: Faugus import + artwork mirroring")
    result = api.post("/api/shortcuts/import_faugus", {"gameid": "stellar-blade"})
    imported = next((g for g in api.ctx.games() if g["name"] == "Stellar Blade"), None)
    check("imported from Faugus", imported is not None and imported["source"] == "faugus")
    if imported:
        check("launch options mirror Faugus",
              imported["launch_options"] == "--game stellar-blade", imported["launch_options"])
        check("exe is quoted like Faugus writes it",
              imported["exe"] == '"/usr/bin/faugus-launcher"', imported["exe"])
    if imported and images:
        hero_image = batch.candidates_for_slot(client, "hero", 5443884)[0]
        api.post("/api/apply", {"appid": imported["appid"], "slot": "hero", "image": hero_image,
                                "sgdb_id": 5443884})
        banner = faugus.BANNERS / "stellar-blade.png"
        check("hero mirrored into Faugus banners", banner.is_file(), str(banner))
        if banner.is_file():
            from PIL import Image

            with Image.open(banner) as banner_image:
                check("banner resized to Faugus' 1920x620", banner_image.size == (1920, 620),
                      str(banner_image.size))
    else:
        skip("Faugus artwork mirroring", "no API key")

    print("\n[10] orphan detection and repair")
    full = next((g for g in api.ctx.games()
                 if all(slot in g["slots"] for slot in ("portrait", "wide", "hero"))), None)
    if full:
        # 1. artwork matching: a stray copy of a live game's art is identified
        grid = api.ctx.grid_dir
        for slot, suffix in (("portrait", "p"), ("wide", ""), ("hero", "_hero")):
            source = Path(full["slots"][slot]["file"])
            shutil.copy2(source, grid / f"8888888888{suffix}{source.suffix}")
        orphans = api.ctx.orphans(fingerprint=True)
        twin = next((o for o in orphans if o["appid"] == "8888888888"), None)
        check("stray artwork is fingerprinted", twin is not None and twin["art_match"] is not None,
              json.dumps(twin)[:160] if twin else "group missing")
        if twin:
            check("fingerprint points at the right game",
                  twin["art_match"]["name"] == full["name"], json.dumps(twin["art_match"]))

        # 2. history repair: art left under an old appid, db remembers the game
        for slot, suffix in (("portrait", "p"), ("wide", ""), ("hero", "_hero")):
            source = Path(full["slots"][slot]["file"])
            shutil.copy2(source, grid / f"9999999999{suffix}{source.suffix}")
        for slot in ("portrait", "wide", "hero"):
            api.post("/api/remove", {"appid": full["appid"], "slot": slot})
        api.ctx.store.remember_appid(full["name"], "9999999999")
        orphans = api.ctx.orphans(fingerprint=True)
        group = next((o for o in orphans if o["appid"] == "9999999999"), None)
        check("orphan group identified from appid history",
              group is not None and group["guess"] == full["name"],
              json.dumps(group)[:160] if group else "group missing")
        check("history orphan is offered for repair",
              bool(group and group["auto"] and group["target_missing"]),
              json.dumps(group)[:160] if group else "")
        plan = batch.auto_repair(api.ctx, dry_run=True, orphans=orphans, confident_only=True)
        check("repair plan includes it",
              any(action["from_appid"] == "9999999999" for action in plan), json.dumps(plan)[:160])
        api.post("/api/orphans/repair", {})
        restored = set(api.ctx.game(full["appid"])["slots"])
        check("repair restores the artwork", {"portrait", "wide", "hero"} <= restored, sorted(restored))
    else:
        skip("orphan tests", "no game with full artwork to build fixtures from")

    print("\n[11] artwork sources")
    listing = {item["id"]: item for item in sources_mod.info(api.ctx)}
    check("steamgriddb listed", "steamgriddb" in listing)
    check("steam source is available", listing["steam"]["available"])
    check("web source is available", listing["web"]["available"])
    check("igdb reports missing credentials or is ready",
          listing["igdb"]["available"] or bool(listing["igdb"]["reason"]))

    some_game = api.ctx.games()[0]
    steam_result = api.get("/api/candidates", {"appid": [some_game["appid"]], "slot": ["portrait"],
                                               "source": ["steam"], "q": ["Cyberpunk 2077"]})
    check("steam returns official art", len(steam_result["images"]) >= 1, json.dumps(steam_result)[:140])
    check("steam candidates carry their source",
          all(image["source"] == "steam" for image in steam_result["images"]))
    if steam_result["images"]:
        applied = api.post("/api/apply", {"appid": some_game["appid"], "slot": "portrait",
                                          "image": steam_result["images"][0]})
        check("applying steam art works", applied["ok"] and Path(applied["files"][0]).is_file())
        record = (api.ctx.store.entry(some_game["name"], create=False) or {}).get("art", {})
        check("art record remembers the source", record.get("portrait", {}).get("source") == "steam")

    iconless = api.get("/api/candidates", {"appid": [some_game["appid"]], "slot": ["icon"],
                                           "source": ["steam"]})
    check("steam refuses unsupported slots", iconless["images"] == [] and "no artwork" in iconless["note"])

    web = api.get("/api/candidates", {"appid": [some_game["appid"]], "slot": ["wide"],
                                      "source": ["web"], "q": ["Hollow Knight wallpaper"]})
    check("web search returns results", len(web["images"]) > 0, web.get("note", "")[:80])
    check("web results point at http urls",
          all(image["url"].startswith("http") for image in web["images"][:5]))

    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (600, 900), (30, 90, 160)).save(buffer, format="PNG")
    upload = api.post("/api/apply_upload", {
        "appid": some_game["appid"], "slot": "icon",
        "data": base64.b64encode(buffer.getvalue()).decode(), "name": "test.png", "mime": "image/png",
    })
    check("uploaded file applied", upload["ok"] and Path(upload["files"][0]).is_file())
    record = (api.ctx.store.entry(some_game["name"], create=False) or {}).get("art", {})
    check("upload recorded as local file", record.get("icon", {}).get("source") == "file")

    try:
        api.post("/api/apply", {"appid": some_game["appid"], "slot": "portrait",
                                "image": {"source": "web", "url": "http://127.0.0.1:1/x.png"}})
        check("loopback downloads are refused", False)
    except Exception as exc:
        check("loopback downloads are refused",
              "non-public" in str(exc) or "download failed" in str(exc), str(exc)[:80])

    print("\n[12] batch with official-first")
    official_game = next((g for g in api.ctx.games() if g["name"] == "Hell is Us"), None)
    if official_game and ctx.config.get("api_key"):
        for slot in ("portrait", "icon"):
            api.post("/api/remove", {"appid": official_game["appid"], "slot": slot})
        job = batch.start_fetch(api.ctx, [official_game["appid"]], ["portrait", "icon"],
                                only_missing=True, prefer_official=True)
        while job.snapshot()["state"] == "running":
            pass
        art = (api.ctx.store.entry("Hell is Us", create=False) or {}).get("art", {})
        check("official-first uses Steam for the portrait",
              art.get("portrait", {}).get("source") == "steam", json.dumps(art.get("portrait", {}))[:110])
        check("official-first falls back for the icon",
              art.get("icon", {}).get("source") == "steamgriddb", json.dumps(art.get("icon", {}))[:110])
    else:
        skip("official-first batch", "no 'Hell is Us' shortcut or no API key")

    print("\n[13] HTTP layer")
    from core import server

    api2, httpd, port = server.serve(0)
    base = f"http://127.0.0.1:{port}"
    try:
        with urllib.request.urlopen(f"{base}/") as response:
            html = response.read().decode()
        check("index.html served", "GridCrate" in html and "window.__GRIDCRATE__" in html)
        with urllib.request.urlopen(f"{base}/static/app.js") as response:
            check("static assets served", response.status == 200)
        try:
            urllib.request.urlopen(f"{base}/api/state")
            check("API rejects missing token", False)
        except urllib.error.HTTPError as exc:
            check("API rejects missing token", exc.code == 403, str(exc.code))
        request = urllib.request.Request(f"{base}/api/state",
                                         headers={"X-GridCrate-Token": api2.token})
        with urllib.request.urlopen(request) as response:
            state = json.loads(response.read())
        check("API works with token", state["steam"]["account_id"] == account_id)
        media = next((g for g in api2.ctx.games() if "portrait" in g["slots"]), None)
        if media:
            with urllib.request.urlopen(
                f"{base}/api/thumb?appid={media['appid']}&slot=portrait&w=320&token={api2.token}"
            ) as response:
                check("thumbnail endpoint", response.headers["Content-Type"] == "image/jpeg")
    except Exception:
        httpd.shutdown()
        httpd.server_close()
        raise

    print("\n[14] security")
    from core import http as http_mod

    # appids are validated before touching the filesystem (they end up in globs)
    for bad in ("*", "../*", "../../etc", "1; rm -rf /", ""):
        try:
            api.post("/api/orphans/delete", {"appids": [bad]})
            check(f"delete refuses appid {bad!r}", False)
        except Exception as exc:
            check(f"delete refuses appid {bad!r}", "invalid appid" in str(exc), str(exc)[:60])
    try:
        api.post("/api/orphans/relink", {"from_appid": "*", "to_appid": "123"})
        check("relink refuses wildcard appid", False)
    except Exception as exc:
        check("relink refuses wildcard appid", "invalid appid" in str(exc), str(exc)[:60])

    grid_files = sorted(p.name for p in Path(api.ctx.grid_dir).iterdir())
    check("nothing was trashed by the wildcard attempts", len(grid_files) > 0, f"{len(grid_files)} files")

    # media endpoints must not build paths from arbitrary strings
    for bad in ("../../etc/passwd", "*", "12/../../x"):
        url = f"{base}/api/thumb?appid={urllib.parse.quote(bad)}&slot=portrait&token={api2.token}"
        try:
            with urllib.request.urlopen(url) as response:
                response.read()
            check(f"thumb refuses appid {bad!r}", False)
        except urllib.error.HTTPError as exc:
            check(f"thumb refuses appid {bad!r}", exc.code == 400, str(exc.code))

    # image sanity guard (decompression bomb)
    import struct as _struct

    tiny = io.BytesIO()
    Image.new("RGB", (2, 2), (0, 0, 0)).save(tiny, format="PNG")
    bomb = bytearray(tiny.getvalue())
    _struct.pack_into(">II", bomb, 16, 30000, 30000)  # patch the IHDR dimensions
    try:
        artwork.check_image_sane(bytes(bomb))
        check("decompression bombs are refused", False)
    except ValueError as exc:
        check("decompression bombs are refused", "large" in str(exc) or "readable" in str(exc), str(exc)[:60])

    # redirects must be re-validated hop by hop (SSRF)
    class FakeResponse:
        status_code = 302
        is_redirect = True
        is_permanent_redirect = False
        headers = {"Location": "http://127.0.0.1:1/secret"}
        url = "https://example.com/x.png"

        def close(self):
            pass

    class FakeSession:
        headers = {}

        def get(self, url, **kwargs):
            return FakeResponse()

    real_session = http_mod.session
    http_mod.session = lambda: FakeSession()
    try:
        http_mod.download_image("https://example.com/x.png")
        check("redirects to private addresses are refused", False)
    except http_mod.DownloadError as exc:
        check("redirects to private addresses are refused", "non-public" in str(exc), str(exc)[:60])
    finally:
        http_mod.session = real_session

    # path traversal in the static handler
    for attempt in ("/static/../core/config.py", "/static/../webx/x", "/static/..%2f..%2fetc%2fpasswd"):
        request = urllib.request.Request(f"{base}{attempt}")
        try:
            with urllib.request.urlopen(request) as response:
                body = response.read()
            check(f"static refuses {attempt}", False, f"got {len(body)} bytes")
        except urllib.error.HTTPError as exc:
            check(f"static refuses {attempt}", exc.code in (403, 404), str(exc.code))

    # oversized request bodies are rejected before being read
    import http.client

    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    connection.putrequest("POST", "/api/apply_upload")
    connection.putheader("X-GridCrate-Token", api2.token)
    connection.putheader("Content-Length", str(200 * 1024 * 1024))
    connection.endheaders()
    response = connection.getresponse()
    check("oversized request bodies rejected", response.status == 413, str(response.status))
    connection.close()

    # security headers
    request = urllib.request.Request(f"{base}/", headers={"X-GridCrate-Token": api2.token})
    with urllib.request.urlopen(request) as response:
        headers = {k.lower(): v for k, v in response.headers.items()}
    check("nosniff header set", headers.get("x-content-type-options") == "nosniff")
    check("no-referrer header set", headers.get("referrer-policy") == "no-referrer")
    check("content security policy set", "default-src 'self'" in headers.get("content-security-policy", ""))

    httpd.shutdown()
    httpd.server_close()

    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed, {len(SKIPPED)} skipped")
    if FAILED:
        print("failed: " + ", ".join(FAILED))
    shutil.rmtree(sandbox, ignore_errors=True)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
