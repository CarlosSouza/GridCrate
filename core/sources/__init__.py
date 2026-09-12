"""Artwork sources.

Each source module exposes:

    ID, LABEL, NOTE, SLOTS, NEEDS
    available(ctx) -> (bool, reason)
    candidates(ctx, game, slot, query=None) -> list[image]

Images are plain dicts so they can travel straight to the UI and back:

    {"source", "id", "url", "thumb", "width", "height", "style",
     "label", "author", "mime", "slot", "game_id"}
"""

from . import igdb, sgdb_source, steam_cdn, websearch

_SOURCES = [sgdb_source, steam_cdn, igdb, websearch]

BY_ID = {source.ID: source for source in _SOURCES}


def image(source, url, thumb=None, width=0, height=0, style="official", label="",
          slot=None, game_id=None, mime="", author="", extra=None):
    payload = {
        "source": source,
        "id": f"{source}:{url}",
        "url": url,
        "thumb": thumb or url,
        "width": width,
        "height": height,
        "style": style,
        "label": label,
        "slot": slot,
        "game_id": game_id,
        "mime": mime,
        "author": author,
    }
    if extra:
        payload.update(extra)
    return payload


def info(ctx):
    result = []
    for source in _SOURCES:
        available, reason = source.available(ctx)
        result.append(
            {
                "id": source.ID,
                "label": source.LABEL,
                "note": source.NOTE,
                "slots": list(source.SLOTS),
                "available": available,
                "reason": reason,
                "needs": list(getattr(source, "NEEDS", ())),
            }
        )
    return result


def candidates(ctx, source_id, game, slot, query=None):
    source = BY_ID.get(source_id or "steamgriddb")
    if source is None:
        raise ValueError(f"unknown source {source_id!r}")
    if slot not in source.SLOTS:
        return {"images": [], "source": source_id,
                "note": f"{source.LABEL} has no artwork for this slot"}
    available, reason = source.available(ctx)
    if not available:
        return {"images": [], "source": source_id, "note": reason, "needs_setup": True}
    result = source.candidates(ctx, game, slot, query=query)
    if isinstance(result, dict):
        return {"source": source_id, **result}
    return {"images": result, "source": source_id}
