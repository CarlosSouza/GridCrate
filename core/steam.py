"""Steam discovery and non-Steam shortcut management.

Layout facts (verified against this machine):
  * shortcut appid = crc32(Exe + AppName) | 0x80000000, stored as signed int32
  * artwork lives in <userdata>/<account>/config/grid/<appid><suffix>.<ext>
  * Steam keeps shortcuts.vdf in memory and rewrites it on exit, so the file
    may only be touched while the client is closed.
"""

import glob
import os
import re
import shutil
import struct
import subprocess
import time
import zlib
from pathlib import Path

from . import vdfbin

STEAM_CANDIDATES = [
    "~/.steam/steam",
    "~/.steam/root",
    "~/.local/share/Steam",
    "~/.var/app/com.valvesoftware.Steam/.steam/steam",
]

MAX_BACKUPS = 10


def crc_appid(exe: str, appname: str) -> int:
    """Unsigned appid used for grid artwork filenames."""
    return zlib.crc32((exe + appname).encode("utf-8")) | 0x80000000


def to_signed(value: int) -> int:
    value &= 0xFFFFFFFF
    return value - 0x100000000 if value >= 0x80000000 else value


def find_steam_root():
    override = os.environ.get("GRIDCRATE_STEAM_ROOT")
    if override and (Path(override) / "userdata").is_dir():
        return Path(override).resolve()
    for candidate in STEAM_CANDIDATES:
        path = Path(os.path.expanduser(candidate))
        if (path / "userdata").is_dir():
            return path.resolve()
    return None


def _read_loginusers(root: Path):
    """account_id -> persona name, from config/loginusers.vdf (text VDF)."""
    names = {}
    path = root / "config/loginusers.vdf"
    if not path.is_file():
        return names
    text = path.read_text(errors="ignore")
    for block in re.findall(r'"(\d{17})"\s*\{(.*?)\n\t\}', text, re.S):
        steamid64, body = block
        persona = re.search(r'"PersonaName"\s*"([^"]*)"', body)
        try:
            account_id = str(int(steamid64) - 76561197960265728)
        except ValueError:
            continue
        names[account_id] = persona.group(1) if persona else account_id
    return names


def list_accounts(root: Path):
    accounts = []
    if not root:
        return accounts
    userdata = root / "userdata"
    if not userdata.is_dir():
        return accounts
    personas = _read_loginusers(root)
    for entry in sorted(userdata.iterdir()):
        if not entry.is_dir() or not entry.name.isdigit() or entry.name == "0":
            continue
        config = entry / "config"
        if not config.is_dir():
            continue
        shortcuts = config / "shortcuts.vdf"
        count = 0
        if shortcuts.is_file():
            try:
                count = len(vdfbin.load(shortcuts).get("shortcuts", {}))
            except Exception:
                count = -1
        accounts.append(
            {
                "account_id": entry.name,
                "persona": personas.get(entry.name, entry.name),
                "shortcuts_path": str(shortcuts),
                "grid_dir": str(config / "grid"),
                "shortcut_count": count,
            }
        )
    return accounts


def pick_default_account(accounts):
    if not accounts:
        return None
    usable = [a for a in accounts if a["shortcut_count"] > 0]
    if usable:
        return max(usable, key=lambda a: a["shortcut_count"])
    return accounts[0]


def steam_running() -> bool:
    """True when the Steam client (not Proton's stub steam.exe) is alive."""
    for pattern in ("steam.sh", "steamwebhelper"):
        try:
            out = subprocess.run(
                ["pgrep", "-f", pattern], capture_output=True, text=True, timeout=5
            ).stdout
        except (OSError, subprocess.SubprocessError):
            return False
        if out.strip():
            return True
    return False


def faugus_running() -> bool:
    try:
        out = subprocess.run(
            ["pgrep", "-f", "faugus-launcher"], capture_output=True, text=True, timeout=5
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return bool(out.strip())


class ShortcutsFile:
    """Read/write access to one account's shortcuts.vdf."""

    def __init__(self, path):
        self.path = Path(path)

    # -- reading ---------------------------------------------------------
    def load(self):
        if not self.path.is_file():
            return {"shortcuts": {}}
        data = vdfbin.load(self.path)
        data.setdefault("shortcuts", {})
        return data

    def games(self):
        """Normalised view of every shortcut in the file."""
        data = self.load()
        result = []
        for index, info in data["shortcuts"].items():
            if not isinstance(info, dict):
                continue
            exe = info.get("Exe", "")
            name = info.get("AppName", "")
            appid = crc_appid(exe, name)
            result.append(
                {
                    "index": index,
                    "appid": str(appid),
                    "appid_signed": to_signed(appid),
                    "name": name,
                    "exe": exe,
                    "launch_options": info.get("LaunchOptions", ""),
                    "start_dir": info.get("StartDir", ""),
                    "icon": info.get("icon", ""),
                    "last_play": info.get("LastPlayTime", 0),
                    "hidden": bool(info.get("IsHidden", 0)),
                    "source": classify_source(exe, info.get("LaunchOptions", "")),
                    "faugus_id": faugus_game_id(info.get("LaunchOptions", "")),
                }
            )
        result.sort(key=lambda g: g["name"].lower())
        return result

    # -- writing ---------------------------------------------------------
    def save(self, data, make_backup=True):
        if steam_running():
            raise RuntimeError(
                "Steam is running. Close it first - the client rewrites "
                "shortcuts.vdf from memory on exit and would discard changes."
            )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if make_backup and self.path.is_file():
            self._backup()
        payload = vdfbin.dumps(data)
        tmp = self.path.with_suffix(".vdf.tmp")
        with open(tmp, "wb") as fh:
            fh.write(payload)
        os.chmod(tmp, 0o755)
        os.replace(tmp, self.path)
        return len(payload)

    def _backup(self):
        stamp = time.strftime("%Y%m%d-%H%M%S")
        target = self.path.with_name(f"{self.path.name}.bak-{stamp}")
        if not target.exists():
            shutil.copy2(self.path, target)
        backups = sorted(
            glob.glob(f"{self.path}.bak-*"), key=os.path.getmtime, reverse=True
        )
        for old in backups[MAX_BACKUPS:]:
            try:
                os.remove(old)
            except OSError:
                pass

    # -- CRUD ------------------------------------------------------------
    def add(self, name, exe, launch_options="", start_dir="", icon=""):
        data = self.load()
        entries = data["shortcuts"]
        numeric = [int(k) for k in entries if str(k).isdigit()]
        index = str(max(numeric) + 1) if numeric else "0"
        entries[index] = {
            "appid": to_signed(crc_appid(exe, name)),
            "AppName": name,
            "Exe": exe,
            "StartDir": start_dir,
            "icon": icon,
            "ShortcutPath": "",
            "LaunchOptions": launch_options,
            "IsHidden": 0,
            "AllowDesktopConfig": 1,
            "AllowOverlay": 1,
            "OpenVR": 0,
            "Devkit": 0,
            "DevkitGameID": "",
            "LastPlayTime": 0,
            "FlatpakAppID": "",
        }
        self.save(data)
        return str(crc_appid(exe, name))

    def update(self, index, **fields):
        data = self.load()
        entry = data["shortcuts"].get(str(index))
        if entry is None:
            raise KeyError(f"shortcut {index} not found")
        for key, value in fields.items():
            if key in ("IsHidden",):
                entry[key] = int(bool(value))
            else:
                entry[key] = value
        if "AppName" in fields or "Exe" in fields:
            entry["appid"] = to_signed(crc_appid(entry.get("Exe", ""), entry.get("AppName", "")))
        self.save(data)
        return entry

    def remove(self, index):
        data = self.load()
        if str(index) not in data["shortcuts"]:
            raise KeyError(f"shortcut {index} not found")
        del data["shortcuts"][str(index)]
        renumber(data["shortcuts"])
        self.save(data)

    def reorder(self):
        data = self.load()
        renumber(data["shortcuts"])
        self.save(data)


def renumber(entries):
    ordered = sorted(
        entries.items(), key=lambda item: int(item[0]) if str(item[0]).isdigit() else 0
    )
    new = {str(i): info for i, (_, info) in enumerate(ordered)}
    entries.clear()
    entries.update(new)


def faugus_game_id(launch_options: str):
    match = re.search(r"--game\s+(\S+)", launch_options or "")
    return match.group(1) if match else None


def classify_source(exe: str, launch_options: str):
    if "faugus-launcher" in exe or faugus_game_id(launch_options):
        return "faugus"
    if "heroic" in exe or "heroic" in (launch_options or ""):
        return "heroic"
    if "lutris" in exe:
        return "lutris"
    if "bottles" in exe:
        return "bottles"
    return "manual"


def grid_dir_for(shortcuts_path):
    return Path(shortcuts_path).parent / "grid"


def ensure_grid_dir(shortcuts_path):
    path = grid_dir_for(shortcuts_path)
    path.mkdir(parents=True, exist_ok=True)
    return path
