"""Web image search, the "Google Images" of Playnite's extensions.

Google blocks scraping without a browser session, so Bing's image endpoint is
used instead - it is the same idea: type a query, see thumbnails in the app and
apply the one you like. Safe search is always on, and results are filtered to
the aspect ratio the slot needs.
"""

import html
import json
import re
from urllib.parse import quote, urlparse

from .. import http

ID = "web"
LABEL = "Web search"
NOTE = "Image search on the web (Bing, safe search on). Unofficial - may rate-limit."
SLOTS = ("portrait", "wide", "hero", "logo", "icon")
NEEDS = ()

_ITEM_RE = re.compile(r'm="({&quot;.*?})"')

SLOT_HINT = {
    "portrait": ("cover art", "tall"),
    "wide": ("key art wallpaper", "wide"),
    "hero": ("key art wallpaper", "wide"),
    "logo": ("logo png", None),
    "icon": ("icon png", None),
}


def available(ctx):
    return True, ""


def default_query(game, slot):
    hint, _ = SLOT_HINT.get(slot, ("artwork", None))
    return f"{game['name']} {hint}".strip()


def candidates(ctx, game, slot, query=None):
    term = (query or default_query(game, slot)).strip()
    _, aspect = SLOT_HINT.get(slot, (None, None))
    params = {
        "q": term,
        "first": 0,
        "count": 40,
        "adlt": "strict",
        "qft": f"+filterui:aspect-{aspect}" if aspect else "",
    }
    try:
        response = http.get("https://www.bing.com/images/async", params=params, timeout=20)
        response.raise_for_status()
    except Exception as exc:
        return {"images": [], "note": f"search failed: {exc}"}

    seen, images = set(), []
    for chunk in _ITEM_RE.findall(response.text):
        try:
            item = json.loads(html.unescape(chunk))
        except ValueError:
            continue
        url = item.get("murl")
        if not url or url in seen or not url.lower().startswith("http"):
            continue
        if urlparse(url).path.lower().endswith(".svg"):
            continue
        seen.add(url)
        host = urlparse(url).hostname or ""
        images.append(
            _build(url, item.get("turl") or url, host, item.get("t") or "")
        )
    return {"images": images, "query": term,
            "note": f"{len(images)} results for “{term}” (Bing, safe search on)"}


def _build(url, thumb, host, title):
    from . import image as build

    return build(ID, url, thumb=thumb, style="web", label=host, author=host,
                 mime="")


def browse_url(term, engine="google"):
    if engine == "bing":
        return f"https://www.bing.com/images/search?q={quote(term)}"
    return f"https://www.google.com/search?tbm=isch&q={quote(term)}"
