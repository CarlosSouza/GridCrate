"""SteamGridDB - the community database with all five slot types."""

from .. import batch, sgdb

ID = "steamgriddb"
LABEL = "SteamGridDB"
NOTE = "Community artwork with every slot type (portrait, wide, hero, logo, icon)."
SLOTS = ("portrait", "wide", "hero", "logo", "icon")
NEEDS = ("api_key",)


def available(ctx):
    if not (ctx.config.get("api_key") or "").strip():
        return False, "Add your SteamGridDB API key in Settings"
    return True, ""


def candidates(ctx, game, slot, query=None):
    client = ctx.sgdb_client()
    sgdb_name = game.get("sgdb_name") or game["name"]
    if query and query.strip().lower() != (game.get("sgdb_name") or "").lower():
        results = client.search(query.strip())
        match, top = sgdb.best_match(query.strip(), results, threshold=0.6)
        if match is None:
            return {"images": [],
                    "needs_review": [{"id": item["id"], "name": item["name"], "source": ID}
                                     for item in results[:8]],
                    "note": f"No SteamGridDB entry matched “{query}”"}
        sgdb_id, sgdb_name = match["id"], match["name"]
    else:
        sgdb_id, sgdb_name, top, error = batch.resolve_sgdb_id(ctx, game, client)
        if error:
            return {"images": [], "note": error}
        if not sgdb_id:
            return {"images": [],
                    "needs_review": [{"id": item["id"], "name": item["name"], "source": ID}
                                     for item in top],
                    "note": f"No confident SteamGridDB match for “{game['name']}”"}

    images = batch.candidates_for_slot(
        client, slot, sgdb_id,
        include_nsfw=bool(ctx.config.get("include_nsfw")),
        include_humor=bool(ctx.config.get("include_humor")),
    )
    for item in images:
        item["source"] = ID
        item["game_id"] = sgdb_id
        item["label"] = item.get("style", "")
    return {"images": images, "matched": {"id": sgdb_id, "name": sgdb_name, "source": ID},
            "note": f"SteamGridDB · {sgdb_name}"}
