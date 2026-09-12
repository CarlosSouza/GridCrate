"""IGDB (Twitch) - the metadata source Playnite uses by default.

Needs a free Twitch application (client id + secret) configured in Settings.
Gives a cover (portrait), artworks and screenshots (wide/hero).
"""

import json
import time

from .. import config, http, sgdb

ID = "igdb"
LABEL = "IGDB"
NOTE = "Covers, artworks and screenshots from IGDB (Twitch). Needs a free Twitch client id + secret."
SLOTS = ("portrait", "wide", "hero")
NEEDS = ("igdb_client_id", "igdb_client_secret")

TOKEN_URL = "https://id.twitch.tv/oauth2/token"
API = "https://api.igdb.com/v4"
IMAGES = "https://images.igdb.com/igdb/image/upload/{size}/{image}.jpg"

SIZES = {"cover_small": "t_cover_big_2x", "1080p": "t_1080p"}


def _token(ctx, force=False):
    client_id = (ctx.config.get("igdb_client_id") or "").strip()
    secret = (ctx.config.get("igdb_client_secret") or "").strip()
    if not client_id or not secret:
        return None, None, "IGDB needs a Twitch client id and secret (Settings)"
    path = config.CACHE_DIR / "igdb_token.json"
    if not force and path.is_file():
        try:
            cached = json.loads(path.read_text())
            if cached.get("client_id") == client_id and cached.get("expires", 0) > time.time() + 60:
                return cached["access_token"], client_id, None
        except (OSError, ValueError):
            pass
    try:
        response = http.session().post(TOKEN_URL, params={
            "client_id": client_id, "client_secret": secret, "grant_type": "client_credentials",
        }, timeout=20)
    except Exception as exc:
        return None, client_id, f"Twitch auth failed: {exc}"
    if not response.ok:
        return None, client_id, f"Twitch auth failed: HTTP {response.status_code}"
    payload = response.json()
    token = payload.get("access_token")
    if not token:
        return None, client_id, "Twitch auth returned no token"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "client_id": client_id, "access_token": token,
            "expires": time.time() + int(payload.get("expires_in", 3600)),
        }))
        path.chmod(0o600)
    except OSError:
        pass
    return token, client_id, None


def available(ctx):
    if not (ctx.config.get("igdb_client_id") and ctx.config.get("igdb_client_secret")):
        return False, "Add your Twitch client id and secret in Settings"
    return True, ""


def _query(ctx, body):
    token, client_id, error = _token(ctx)
    if error:
        return None, error
    try:
        response = http.session().post(
            f"{API}/games", data=body.encode(),
            headers={"Client-ID": client_id, "Authorization": f"Bearer {token}",
                     "Accept": "application/json"},
            timeout=20,
        )
    except Exception as exc:
        return None, f"IGDB request failed: {exc}"
    if response.status_code == 401:
        token, client_id, error = _token(ctx, force=True)
        if error:
            return None, error
        response = http.session().post(
            f"{API}/games", data=body.encode(),
            headers={"Client-ID": client_id, "Authorization": f"Bearer {token}"},
            timeout=20,
        )
    if not response.ok:
        return None, f"IGDB error {response.status_code}"
    try:
        return response.json(), None
    except ValueError:
        return None, "IGDB returned invalid JSON"


def search(ctx, term, limit=8):
    payload, error = _query(ctx, f'search "{term}"; fields name,cover.url; limit {limit};')
    if error:
        return [], error
    return [{"id": item["id"], "name": item.get("name", ""),
             "thumb": _image_url((item.get("cover") or {}).get("url"), "cover_small")}
            for item in payload], None


def candidates(ctx, game, slot, query=None):
    term = (query or game["name"]).strip()
    entry = ctx.store.entry(game["name"], create=False) or {}
    igdb_id = entry.get("igdb_id")
    if not igdb_id:
        body = (f'search "{term}"; fields name,cover.url,artworks.url,screenshots.url; limit 10;')
        payload, error = _query(ctx, body)
        if error:
            return {"images": [], "note": error}
        match, top = sgdb.best_match(term, [{"id": item["id"], "name": item.get("name", ""),
                                             "cover": item.get("cover")} for item in payload], 0.72)
        if match is None:
            return {"images": [],
                    "needs_review": [{"id": item["id"], "name": item.get("name", ""), "source": ID}
                                     for item in payload[:6]],
                    "note": f"No IGDB entry matched “{term}”"}
        record = next(item for item in payload if item["id"] == match["id"])
        ctx.store.set_field(game["name"], "igdb_id", record["id"])
        ctx.store.set_field(game["name"], "igdb_name", record.get("name", ""))
    else:
        payload, error = _query(ctx, f'fields name,cover.url,artworks.url,screenshots.url; where id = {int(igdb_id)};')
        if error or not payload:
            return {"images": [], "note": error or "IGDB entry not found"}
        record = payload[0]

    images = []
    cover = (record.get("cover") or {}).get("url")
    if slot == "portrait" and cover:
        images.append(_build(cover, "cover_small", 528, 748, "IGDB cover", record["id"]))
    if slot in ("wide", "hero"):
        for index, artwork in enumerate(record.get("artworks") or []):
            images.append(_build(artwork.get("url"), "1080p", 1920, 1080,
                                 f"artwork {index + 1}", record["id"]))
        for index, shot in enumerate(record.get("screenshots") or []):
            images.append(_build(shot.get("url"), "1080p", 1920, 1080,
                                 f"screenshot {index + 1}", record["id"]))
    return {"images": images, "matched": {"id": record["id"], "name": record.get("name", ""),
                                          "source": ID},
            "note": f"IGDB · {record.get('name', '')}"}


def _image_url(url, size):
    if not url:
        return ""
    return IMAGES.format(size=SIZES.get(size, "t_cover_big_2x"), image=url.rsplit("/", 1)[-1])


def _build(url, size, width, height, label, game_id):
    from . import image as build

    full = _image_url(url, size)
    return build(ID, full, thumb=full, width=width, height=height, style="official",
                 label=label, author="IGDB", mime="image/jpeg", game_id=game_id)
