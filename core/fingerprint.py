"""Perceptual fingerprints used to identify orphaned artwork.

When Faugus/Heroic rewrite shortcuts.vdf the appid changes and the old artwork
is left behind with no name attached.  A dHash (difference hash) comparison
against the artwork of live games, the icons Faugus stores per game, and the
other orphan groups is usually enough to say which game a stray file belongs to.
"""

import json
import os
from pathlib import Path

from PIL import Image

STRONG = 6
WEAK = 10
HASH_SIZE = 8

# Logos are transparent PNGs: after flattening they all look alike, so they are
# never used as evidence. Icons are simple shapes and need a tighter bound.
SLOT_THRESHOLD = {"portrait": STRONG, "wide": STRONG, "hero": STRONG, "icon": 4}
USABLE_SLOTS = tuple(SLOT_THRESHOLD)


def threshold_for(slot):
    return SLOT_THRESHOLD.get(slot)


def dhash(path, size=HASH_SIZE):
    try:
        with Image.open(path) as image:
            image = image.convert("L").resize((size + 1, size), Image.LANCZOS)
            pixels = list(image.getdata())
    except Exception:
        return None
    bits = 0
    for row in range(size):
        offset = row * (size + 1)
        for col in range(size):
            bits = (bits << 1) | (1 if pixels[offset + col] < pixels[offset + col + 1] else 0)
    return bits


def hamming(a, b):
    return bin(a ^ b).count("1")


class Hashes:
    """dHash cache keyed by path+mtime, so repeated scans stay cheap."""

    def __init__(self, cache_path):
        self.cache_path = Path(cache_path)
        self.entries = {}
        self.dirty = False
        if self.cache_path.is_file():
            try:
                self.entries = json.loads(self.cache_path.read_text())
            except (OSError, ValueError):
                self.entries = {}

    def get(self, path):
        path = Path(path)
        try:
            stat = path.stat()
        except OSError:
            return None
        key = f"{path}:{stat.st_mtime_ns}:{stat.st_size}"
        if key in self.entries:
            value = self.entries[key]
            return value if value is not None else None
        value = dhash(path)
        self.entries[key] = value
        self.dirty = True
        if len(self.entries) > 4000:
            self.entries = dict(list(self.entries.items())[-2000:])
        return value

    def save(self):
        if not self.dirty:
            return
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.cache_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.entries))
        os.replace(tmp, self.cache_path)
        self.dirty = False


def best_match(probe, references, limit=3):
    """references: list of dicts with a 'hash' key."""
    if probe is None:
        return []
    scored = [
        {"distance": hamming(probe, ref["hash"]), **{k: v for k, v in ref.items() if k != "hash"}}
        for ref in references
        if ref.get("hash") is not None
    ]
    scored.sort(key=lambda item: item["distance"])
    return scored[:limit]
