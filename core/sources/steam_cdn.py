"""Official artwork straight from Steam's CDN.

Non-Steam shortcuts often point at games that do exist on Steam, and Valve
publishes exactly the sizes the grid wants (600x900 portrait, 460x215 header,
1920x620 hero, transparent logo). No API key needed.
"""

from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote

from .. import http, sgdb

ID = "steam"
LABEL = "Steam"
NOTE = "Official art from Steam's CDN - needs the game to exist on Steam."
SLOTS = ("portrait", "wide", "hero", "logo")
NEEDS = ()

CDN = "https://cdn.cloudflare.steamstatic.com/steam/apps/{appid}/{asset}"

ASSETS = {
    "portrait": [
        ("library_600x900_2x.jpg", 1200, 1800, "library 1200x1800"),
        ("library_600x900.jpg", 600, 900, "library 600x900"),
    ],
    "wide": [
        ("header.jpg", 460, 215, "header 460x215"),
        ("capsule_616x353.jpg", 616, 353, "capsule 616x353"),
    ],
    "hero": [
        ("library_hero.jpg", 1920, 620, "library hero 1920x620"),
    ],
    "logo": [
        ("logo.png", 0, 0, "official logo"),
    ],
}


def available(ctx):
    return True, ""


def search(term, limit=12):
    if not term.strip():
        return []
    response = http.get("https://store.steampowered.com/api/storesearch/",
                        params={"term": term.strip(), "l": "en", "cc": "US"})
    if not response.ok:
        return []
    try:
        items = response.json().get("items") or []
    except ValueError:
        return []
    return [
        {"id": item.get("id"), "name": item.get("name", ""),
         "thumb": item.get("tiny_image", ""), "type": item.get("type")}
        for item in items[:limit]
        if item.get("id")
    ]


def _exists(url):
    try:
        response = http.session().head(url, timeout=12, allow_redirects=True)
        if response.status_code == 405:  # some edges refuse HEAD
            response = http.session().get(url, timeout=12, stream=True)
            response.close()
        return response.status_code < 400
    except Exception:
        return False


def resolve_appid(ctx, game, query=None):
    """Steam appid for a game, cached in the local database."""
    entry = ctx.store.entry(game["name"], create=False) or {}
    if entry.get("steam_appid"):
        return int(entry["steam_appid"]), entry.get("steam_name") or game["name"], []
    results = search(query or game["name"])
    match, top = sgdb.best_match(query or game["name"], results, threshold=0.72)
    if match is None:
        return None, None, top
    ctx.store.set_field(game["name"], "steam_appid", match["id"])
    ctx.store.set_field(game["name"], "steam_name", match["name"])
    return match["id"], match["name"], top


def candidates(ctx, game, slot, query=None):
    appid, name, top = resolve_appid(ctx, game, query=query)
    if appid is None:
        return {
            "images": [],
            "needs_review": [{"id": item["id"], "name": item["name"], "source": ID} for item in top],
            "note": f"No Steam page matched “{query or game['name']}”",
        }
    assets = ASSETS.get(slot) or []
    urls = [(asset, width, height, label) for asset, width, height, label in assets]
    with ThreadPoolExecutor(max_workers=4) as pool:
        alive = list(pool.map(lambda item: _exists(CDN.format(appid=appid, asset=item[0])), urls))
    images = [
        _image(appid, asset, width, height, label)
        for (asset, width, height, label), ok in zip(urls, alive) if ok
    ]
    return {
        "images": images,
        "matched": {"id": appid, "name": name, "source": ID},
        "note": f"Steam app {appid} · {name}" if images else "Nothing found on Steam's CDN",
    }


def _image(appid, asset, width, height, label):
    from . import image as build

    url = CDN.format(appid=appid, asset=asset)
    return build(ID, url, thumb=url, width=width, height=height, style="official",
                 label=label, author="Valve", mime="image/jpeg" if asset.endswith(".jpg") else "image/png",
                 game_id=appid)


def search_url(term):
    return f"https://store.steampowered.com/search/?term={quote(term)}"
