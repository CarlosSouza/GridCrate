"""Background jobs: bulk artwork fetching and orphan auto-repair."""

import threading
import time

from . import artwork, sgdb

PICK_PREFERENCE = {"official": 0, "alternate": 1, "white_logo": 2, "no_logo": 3, "material": 4, "blur": 5}


def pick_best(images):
    if not images:
        return None
    return sorted(
        images,
        key=lambda image: (
            PICK_PREFERENCE.get(image.get("style", ""), 9),
            -int(image.get("upvotes") or 0),
        ),
    )[0]


def candidates_for_slot(client, slot, sgdb_id, include_nsfw=False, include_humor=False, styles=None):
    spec = artwork.SLOTS[slot]
    dimensions = [spec["dimensions"]] + list(spec.get("alts") or []) if spec.get("dimensions") else [None]
    for dimension in dimensions:
        images = client.images(
            spec["kind"],
            sgdb_id,
            dimensions=dimension,
            styles=styles,
            nsfw=include_nsfw,
            humor=include_humor,
        )
        if images:
            return images
    return []


def resolve_sgdb_id(ctx, game, client, threshold=0.72):
    """Return (sgdb_id, sgdb_name, top_candidates, error)."""
    entry = ctx.store.entry(game["name"], create=False) or {}
    if entry.get("sgdb_id"):
        return entry["sgdb_id"], entry.get("sgdb_name") or game["name"], [], None
    try:
        results = client.search(game["name"])
    except sgdb.SGDBError as exc:
        return None, None, [], str(exc)
    match, top = sgdb.best_match(game["name"], results, threshold)
    if match is None:
        return None, None, top, None
    ctx.store.set_sgdb(game["name"], match["id"], match["name"])
    return match["id"], match["name"], top, None


class Job:
    """A cancellable background task with a pollable snapshot."""

    def __init__(self, kind, total):
        self.kind = kind
        self.total = total
        self.done = 0
        self.state = "running"
        self.started = time.time()
        self.finished = None
        self.items = []
        self.error = None
        self._cancel = threading.Event()
        self._lock = threading.Lock()
        self._thread = None

    def start(self, target):
        self._thread = threading.Thread(target=target, daemon=True, name=f"gridcrate-{self.kind}")
        self._thread.start()
        return self

    def cancel(self):
        self._cancel.set()

    @property
    def cancelled(self):
        return self._cancel.is_set()

    def add(self, **item):
        with self._lock:
            self.items.append(item)
            self.done += 1

    def finish(self, state="done"):
        with self._lock:
            self.state = state
            self.finished = time.time()

    def snapshot(self):
        with self._lock:
            return {
                "kind": self.kind,
                "state": self.state,
                "total": self.total,
                "done": self.done,
                "items": list(self.items),
                "error": self.error,
                "started": self.started,
                "finished": self.finished,
            }


def official_candidates(ctx, game, slot):
    """Official Steam art for a slot, when the game exists on Steam."""
    from .sources import steam_cdn

    if slot not in steam_cdn.SLOTS:
        return []
    try:
        result = steam_cdn.candidates(ctx, game, slot)
    except Exception:
        return []
    return result.get("images") or []


def start_fetch(ctx, appids, slots, only_missing=True, threshold=0.72, faugus=None,
                overwrite=False, prefer_official=False):
    games = [g for g in ctx.games() if (not appids or g["appid"] in set(appids))]
    job = Job("fetch", len(games))
    client = ctx.sgdb_client()

    def run():
        try:
            for game in games:
                if job.cancelled:
                    job.finish("cancelled")
                    return
                fresh = ctx.game(game["appid"]) or game
                target_slots = [
                    s for s in slots if overwrite or not only_missing or s in fresh["missing"]
                ]
                if not target_slots:
                    job.add(appid=game["appid"], name=game["name"], status="skipped",
                            detail="nothing missing")
                    continue
                if prefer_official:
                    official_slots = [s for s in target_slots if s in ("portrait", "wide", "hero", "logo")]
                    if official_slots:
                        applied_official, failed_official = [], []
                        for slot in official_slots:
                            if job.cancelled:
                                job.finish("cancelled")
                                return
                            images = official_candidates(ctx, fresh, slot)
                            if not images:
                                failed_official.append(slot)
                                continue
                            try:
                                ctx.apply_image(fresh, slot, images[0], also_faugus=faugus)
                                applied_official.append(slot)
                            except (Exception) as exc:  # noqa: BLE001
                                failed_official.append(f"{slot}: {exc}")
                        target_slots = [s for s in target_slots if s not in applied_official]
                        if applied_official and not target_slots:
                            job.add(appid=game["appid"], name=game["name"], status="ok",
                                    slots=applied_official, detail="official Steam art",
                                    source="steam")
                            continue
                sgdb_id, sgdb_name, top, error = resolve_sgdb_id(ctx, fresh, client, threshold)
                if error:
                    job.add(appid=game["appid"], name=game["name"], status="error", detail=error)
                    continue
                if not sgdb_id:
                    job.add(
                        appid=game["appid"],
                        name=game["name"],
                        status="needs_review",
                        detail="no confident SteamGridDB match",
                        candidates=[{"id": c["id"], "name": c["name"]} for c in top],
                    )
                    continue
                applied, problems = [], []
                for slot in target_slots:
                    if job.cancelled:
                        job.finish("cancelled")
                        return
                    try:
                        images = candidates_for_slot(
                            client, slot, sgdb_id,
                            include_nsfw=bool(ctx.config.get("include_nsfw")),
                            include_humor=bool(ctx.config.get("include_humor")),
                        )
                        image = pick_best(images)
                        if image is None:
                            problems.append(f"{slot}: no images")
                            continue
                        ctx.apply_image(
                            fresh, slot, image, sgdb_id=sgdb_id, sgdb_name=sgdb_name,
                            also_faugus=faugus, client=client,
                        )
                        applied.append(slot)
                    except (sgdb.SGDBError, OSError) as exc:
                        problems.append(f"{slot}: {exc}")
                status = "ok" if applied and not problems else ("partial" if applied else "failed")
                job.add(
                    appid=game["appid"], name=game["name"], status=status,
                    slots=applied, detail="; ".join(problems), sgdb_id=sgdb_id,
                    sgdb_name=sgdb_name,
                )
                time.sleep(0.2)
            job.finish("done")
        except Exception as exc:  # keep the UI informed instead of dying silently
            job.error = str(exc)
            job.finish("error")

    return job.start(run)


CONFIDENT_SOURCES = ("history", "boilr", "artwork-twin")


def auto_repair(ctx, dry_run=False, orphans=None, confident_only=True):
    """Re-link orphaned artwork to the live appid of the same game.

    Uses the appid history kept in the local database (and BoilR's cache as a
    fallback guess), which is exactly the failure mode that leaves a game
    without art after Faugus/Heroic rewrite shortcuts.vdf.
    """
    actions = []
    for orphan in orphans if orphans is not None else ctx.orphans():
        target = orphan.get("target_appid")
        if not target:
            continue
        if confident_only and orphan.get("guess_source") not in CONFIDENT_SOURCES:
            continue
        missing = [s for s in orphan.get("target_missing", []) if s in orphan.get("slots", {})]
        if not missing:
            continue
        action = {
            "from_appid": orphan["appid"],
            "to_appid": target,
            "name": orphan.get("target_name") or orphan.get("guess"),
            "slots": missing,
        }
        if not dry_run:
            action["files"] = artwork.relink(
                ctx.grid_dir,
                orphan["appid"],
                target,
                slots=missing,
                move=bool(ctx.config.get("trash_after_relink")),
                trash_root=None,
            )
            game = ctx.game(target)
            if game:
                ctx.store.remember_appid(game["name"], target)
        actions.append(action)
    return actions
