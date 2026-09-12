"""Faugus Launcher integration.

Faugus keeps its own artwork next to games.json:

    covers/<gameid>.png    460x690 portrait, path also stored in games.json
    banners/<gameid>.png   1920x620 banner
    icons/<gameid>.png     square icon (256px), path stored in games.json

Writing images is safe while Faugus runs, but games.json is rewritten by the
GUI on save - it must be closed before touching the JSON.
"""

import json
import os
import time
from pathlib import Path

from PIL import Image

from .sgdb import normalize_name

BASE = Path(os.environ.get("GRIDCRATE_FAUGUS_BASE")
            or os.path.expanduser("~/.local/share/faugus-launcher"))
GAMES_JSON = BASE / "games.json"
COVERS = BASE / "covers"
BANNERS = BASE / "banners"
ICONS = BASE / "icons"

COVER_SIZE = (460, 690)
BANNER_SIZE = (1920, 620)
ICON_SIZE = (256, 256)


def available():
    return GAMES_JSON.is_file()


def load_games():
    if not GAMES_JSON.is_file():
        return []
    try:
        return json.loads(GAMES_JSON.read_text())
    except (OSError, ValueError):
        return []


def save_games(games, running_check=None):
    if running_check and running_check():
        raise RuntimeError(
            "Faugus Launcher is running - close it first, it overwrites games.json on save."
        )
    tmp = GAMES_JSON.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(games, indent=1, ensure_ascii=False))
    os.replace(tmp, GAMES_JSON)


def find_game(games, gameid=None, title=None):
    if gameid:
        for game in games:
            if game.get("gameid") == gameid:
                return game
    if title:
        key = normalize_name(title)
        for game in games:
            if normalize_name(game.get("title", "")) == key:
                return game
    return None


def _resize(source, destination, size):
    destination.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(source) as image:
        image = image.convert("RGBA")
        fitted = image.copy()
        fitted.thumbnail(size, Image.LANCZOS)
        canvas = Image.new("RGBA", size, (0, 0, 0, 0))
        canvas.paste(fitted, ((size[0] - fitted.width) // 2, (size[1] - fitted.height) // 2))
        canvas.save(destination, format="PNG")
    return destination


def apply_art(game, portrait=None, hero=None, icon=None):
    """Mirror applied Steam artwork into Faugus' own folders."""
    gameid = game.get("gameid")
    if not gameid:
        return {}
    written = {}
    if portrait and Path(portrait).is_file():
        written["cover"] = str(_resize(portrait, COVERS / f"{gameid}.png", COVER_SIZE))
    if hero and Path(hero).is_file():
        written["banner"] = str(_resize(hero, BANNERS / f"{gameid}.png", BANNER_SIZE))
    if icon and Path(icon).is_file():
        icon_dir = Path(os.path.dirname(game.get("icon") or (ICONS / f"{gameid}.png")))
        written["icon"] = str(_resize(icon, icon_dir / f"{gameid}.png", ICON_SIZE))
    return written


def remember_metadata(game, sgdb_id=None, cover_path=None, running_check=None):
    """Persist steamgriddb_id / cover in games.json (needs Faugus closed)."""
    games = load_games()
    entry = find_game(games, gameid=game.get("gameid"))
    if entry is None:
        return False
    changed = False
    if sgdb_id and str(entry.get("steamgriddb_id") or "") != str(sgdb_id):
        entry["steamgriddb_id"] = str(sgdb_id)
        changed = True
    if cover_path and Path(cover_path).is_file() and entry.get("cover") != cover_path:
        entry["cover"] = cover_path
        changed = True
    if not changed:
        return False
    save_games(games, running_check)
    return True


def backup_games_json():
    if not GAMES_JSON.is_file():
        return None
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = GAMES_JSON.with_name(f"games.json.bak-{stamp}")
    if not target.exists():
        target.write_bytes(GAMES_JSON.read_bytes())
    return target
