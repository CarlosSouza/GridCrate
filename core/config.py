"""Paths and persisted settings."""

import json
import os
import re
from pathlib import Path

def _dir(env, default):
    """Env overrides exist so the whole app can be exercised in a sandbox."""
    return Path(os.environ.get(env) or os.path.expanduser(default))


# The app runs from its own checkout, so the code directory is derived from
# this file instead of a fixed install path.
APP_DIR = Path(os.environ.get("GRIDCRATE_APP_DIR") or Path(__file__).resolve().parent.parent)
CONFIG_DIR = _dir("GRIDCRATE_CONFIG_DIR", "~/.config/gridcrate")
CACHE_DIR = _dir("GRIDCRATE_CACHE_DIR", "~/.cache/gridcrate")
CONFIG_PATH = CONFIG_DIR / "config.json"
DB_PATH = CONFIG_DIR / "db.json"
RUNTIME_PATH = CACHE_DIR / "runtime.json"
TRASH_DIR = CACHE_DIR / "trash"
THUMB_DIR = CACHE_DIR / "thumbs"
WEB_DIR = APP_DIR / "web"

DEFAULTS = {
    "api_key": "",
    "account_id": None,
    "faugus_integration": True,
    "auto_repair": True,
    "trash_after_relink": False,
    "prefer_official": False,
    "igdb_client_id": "",
    "igdb_client_secret": "",
    "include_nsfw": False,
    "include_humor": False,
    "grid_dir_override": None,
}


def load():
    config = dict(DEFAULTS)
    if CONFIG_PATH.is_file():
        try:
            config.update(json.loads(CONFIG_PATH.read_text()))
        except (OSError, ValueError):
            pass
    if not config.get("api_key"):
        config["api_key"] = _seed_api_key()
    return config


def save(config):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    payload = {key: config.get(key, default) for key, default in DEFAULTS.items()}
    tmp = CONFIG_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=1))
    os.replace(tmp, CONFIG_PATH)
    os.chmod(CONFIG_PATH, 0o600)
    return payload


def _seed_api_key():
    """Borrow the key BoilR/Faugus already use, so first run just works."""
    boilr = Path(os.path.expanduser("~/.config/boilr/config.toml"))
    if boilr.is_file():
        match = re.search(r'auth_key\s*=\s*"([^"]+)"', boilr.read_text(errors="ignore"))
        if match:
            return match.group(1).strip()
    faugus = Path(os.path.expanduser("~/.config/faugus-launcher/config.json"))
    if faugus.is_file():
        try:
            key = json.loads(faugus.read_text()).get("steamgriddb-api-key", "")
            if key:
                return key.strip().strip('"')
        except (OSError, ValueError):
            pass
    return ""


def write_runtime(payload):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = RUNTIME_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload))
    os.chmod(tmp, 0o600)
    os.replace(tmp, RUNTIME_PATH)


def read_runtime():
    try:
        return json.loads(RUNTIME_PATH.read_text())
    except (OSError, ValueError):
        return {}
