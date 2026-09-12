"""SteamGridDB API v2 client.

Quirks found while probing the API:
  * the CDN/API rejects urllib's default user agent (403) - requests is fine
  * `mimes` must be a full mime type ("image/png"), not "png"
  * `dimensions` accepts a single size per request reliably; query one at a time
  * animated assets are webm/mp4, which Steam does not accept for shortcuts,
    so `types=static` is always sent
"""

import re
import unicodedata
from difflib import SequenceMatcher

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

API = "https://www.steamgriddb.com/api/v2"
CDN_UA = "GridCrate/1.0 (+https://github.com/PhilipK/BoilR)"

KINDS = ("grids", "heroes", "logos", "icons")


class SGDBError(Exception):
    pass


def _session():
    session = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=0.4,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
    )
    adapter = HTTPAdapter(max_retries=retry, pool_maxsize=16)
    session.mount("https://", adapter)
    session.headers.update({"User-Agent": CDN_UA, "Accept": "application/json"})
    return session


class SteamGridDB:
    def __init__(self, api_key, timeout=15):
        self.api_key = (api_key or "").strip()
        self.timeout = timeout
        self.session = _session()

    # -- plumbing --------------------------------------------------------
    def _get(self, path, params=None):
        if not self.api_key:
            raise SGDBError("No SteamGridDB API key configured")
        try:
            response = self.session.get(
                f"{API}{path}",
                params=params or {},
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise SGDBError(f"network error: {exc}") from exc
        if response.status_code == 401:
            raise SGDBError("SteamGridDB rejected the API key (401)")
        if response.status_code == 403:
            raise SGDBError("SteamGridDB blocked the request (403)")
        if response.status_code == 404:
            return []
        if not response.ok:
            raise SGDBError(f"SteamGridDB error {response.status_code}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise SGDBError("invalid JSON from SteamGridDB") from exc
        return payload.get("data") or []

    # -- endpoints -------------------------------------------------------
    def search(self, term):
        if not term.strip():
            return []
        data = self._get(f"/search/autocomplete/{requests.utils.quote(term.strip())}")
        return [
            {
                "id": item.get("id"),
                "name": item.get("name", ""),
                "verified": bool(item.get("verified")),
                "types": item.get("types") or [],
                "release_date": item.get("release_date"),
            }
            for item in data
            if item.get("id")
        ]

    def images(
        self,
        kind,
        game_id,
        dimensions=None,
        styles=None,
        mimes=None,
        nsfw=False,
        humor=False,
        epilepsy=False,
        limit=None,
    ):
        if kind not in KINDS:
            raise SGDBError(f"unknown asset kind {kind!r}")
        params = {"types": "static"}
        if kind == "grids" and dimensions:
            params["dimensions"] = dimensions
        if styles:
            params["styles"] = ",".join(styles)
        if mimes:
            params["mimes"] = ",".join(mimes)
        if not nsfw:
            params["nsfw"] = "false"
        if not humor:
            params["humor"] = "false"
        if not epilepsy:
            params["epilepsy"] = "false"
        data = self._get(f"/{kind}/game/{game_id}", params)
        images = [normalize_image(item, kind) for item in data]
        return images[:limit] if limit else images

    def download(self, url):
        try:
            response = self.session.get(url, timeout=self.timeout * 2)
            response.raise_for_status()
        except requests.RequestException as exc:
            raise SGDBError(f"download failed: {exc}") from exc
        return response.content

    def test_key(self):
        try:
            results = self.search("portal")
        except SGDBError as exc:
            return False, str(exc)
        return (bool(results), "ok" if results else "no results")


def normalize_image(item, kind):
    author = item.get("author") or {}
    return {
        "id": item.get("id"),
        "kind": kind,
        "url": item.get("url", ""),
        "thumb": item.get("thumb") or item.get("url", ""),
        "width": item.get("width") or 0,
        "height": item.get("height") or 0,
        "style": item.get("style") or "unknown",
        "mime": item.get("mime") or "",
        "language": item.get("language") or "",
        "nsfw": bool(item.get("nsfw")),
        "humor": bool(item.get("humor")),
        "epilepsy": bool(item.get("epilepsy")),
        "author": author.get("name", "") if isinstance(author, dict) else str(author),
        "upvotes": item.get("upvotes", 0),
        "downvotes": item.get("downvotes", 0),
    }


# -- name matching -------------------------------------------------------

_EDITION_NOISE = re.compile(
    r"\b(goty|game of the year|definitive|deluxe|ultimate|complete|enhanced|"
    r"remaster(ed)?|edition|special|gold|premium|standard|digital|"
    r"early access|demo|beta)\b",
    re.I,
)


def normalize_name(name: str) -> str:
    name = unicodedata.normalize("NFKD", name)
    name = "".join(ch for ch in name if not unicodedata.combining(ch))
    name = _EDITION_NOISE.sub(" ", name)
    name = re.sub(r"[^a-z0-9]+", " ", name.lower())
    return " ".join(name.split())


def match_score(query: str, candidate: str) -> float:
    a, b = normalize_name(query), normalize_name(candidate)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    ratio = SequenceMatcher(None, a, b).ratio()
    if a in b or b in a:
        ratio = max(ratio, 0.85 * min(len(a), len(b)) / max(len(a), len(b)) + 0.15)
    return round(ratio, 4)


def best_match(query, results, threshold=0.72):
    scored = sorted(
        ((match_score(query, r["name"]), r) for r in results),
        key=lambda pair: (-pair[0], not pair[1].get("verified")),
    )
    if not scored or scored[0][0] < threshold:
        return None, scored[:5]
    return scored[0][1], scored[:5]
