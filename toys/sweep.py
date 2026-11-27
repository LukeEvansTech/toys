"""One sweep: read every shop, store what it showed, judge what is new, mail what is worth
showing.

The order matters. A listing is marked seen only after the mail that carried it went, so a
failed send leaves it new for the next sweep. A shop read for the first time is recorded at
once and announces nothing. A listing the model did not answer for is neither mailed nor
marked seen: it is asked about again next time.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import threading
from pathlib import Path
from zoneinfo import ZoneInfo

from collectorkit import collect, images, mail, notify, runs, seen
from collectorkit.db import DB
from collectorkit.llm import LLM
from jinja2 import Environment, FileSystemLoader, select_autoescape

from . import judge, profile, sources
from .config import Settings

log = logging.getLogger(__name__)
LOCK = threading.Lock()  # sweeps never overlap
ENV = Environment(
    loader=FileSystemLoader(Path(__file__).parent / "templates"),
    autoescape=select_autoescape(["html"]),
)
MAX_PICKS, MAX_REST = 20, 40
MAX_DETAIL_TRIES = 3
HEARTBEAT = "heartbeat_week"
LAST_MAIL = "last_mail"


def today(timezone: str) -> dt.datetime:
    return dt.datetime.now(ZoneInfo(timezone))


def sig(name: str, tags: list[str]) -> str:
    """What a verdict was given for: the title and the tags."""
    return hashlib.sha256(json.dumps([name, tags]).encode()).hexdigest()[:16]


def full(raw: bytes, img_dir: Path) -> str:
    """A large copy for the listing's own page. A stored photo is named by the original's
    hash whatever its size, so this copy has its own name beside the small one."""
    jpeg = images.as_jpeg(raw, 1600)
    if jpeg is None:
        raise ValueError("not an image")
    path = img_dir / (hashlib.sha256(raw).hexdigest()[:24] + "-l.jpg")
    if not path.exists():
        img_dir.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".{threading.get_ident()}.tmp")
        tmp.write_bytes(jpeg)
        tmp.replace(path)
    return path.name


def store(db: DB, items: list[collect.Item]) -> None:
    stamp = runs.now()
    with db.tx() as c:
        c.executemany(
            "INSERT INTO listings (source, key, name, url, price, image, tags, first_seen,"
            " last_seen) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(source, key) DO UPDATE SET name = excluded.name, url = excluded.url,"
            " price = excluded.price, image = excluded.image, tags = excluded.tags,"
            " last_seen = excluded.last_seen",
            [
                (
                    i.source,
                    i.key,
                    i.name,
                    i.url,
                    i.price,
                    i.image,
                    json.dumps(i.tags),
                    stamp,
                    stamp,
                )
                for i in items
            ],
        )
        # A shop can rename a listing after it was judged. In the same write, a verdict given
        # for another title or other tags stops counting: the listing is not shown again
        # until the model has answered for these.
        c.executemany(
            "UPDATE listings SET judged = NULL, liked = NULL WHERE source = ? AND key = ?"
            " AND judged_as != '' AND judged_as != ? AND dropped = 0",
            [(i.source, i.key, sig(i.name, i.tags)) for i in items],
        )


def pending(db: DB, source: str, showing: set[str]) -> list[collect.Item]:
    """Listings still owed a verdict or a mail that the shop no longer shows on the page
    read: held over a cap, unanswered, or in a mail that failed, and since pushed off it."""
    rows = db.all(
        "SELECT * FROM listings l WHERE l.source = ? AND NOT EXISTS (SELECT 1 FROM seen x"
        " WHERE x.source = l.source AND x.key = l.key)",
        source,
    )
    return [
        collect.Item(
            r["key"], r["name"], r["url"], r["price"], r["image"], json.loads(r["tags"]), source
        )
        for r in rows
        if r["key"] not in showing
    ]


def row(db: DB, i: collect.Item) -> dict:
    r = db.one("SELECT * FROM listings WHERE source = ? AND key = ?", i.source, i.key)
    return dict(r) if r else {}


def photo(db: DB, s: Settings, listing: dict) -> str:
    """The stored copy of the listing's first photo, fetched once; '' when there is none."""
    if listing["photo"] and (s.img_dir / listing["photo"]).is_file():
        return listing["photo"]
    name = images.download(listing["image"], s.img_dir, referer=listing["url"])
    if name:
        db.run("UPDATE listings SET photo = ? WHERE id = ?", name, listing["id"])
    return name


def fill_detail(db: DB, s: Settings, model: LLM, listing: dict) -> dict:
    """Fetch a listing's own page: every photo at full size, the description, and the facts
    in it. The listing's verdict covered its first photo only, so the page's photos are shown
    once the model has cleared them, and one that is not clear drops the whole listing
    (``{"dropped": True}``). Kept once it is whole; a read that missed a photo or an answer
    is tried again on a later view, a few times. Returns what there is, {} when nothing."""
    old = json.loads(listing["detail"]) if listing["detail"] else {}
    if old and (old.get("complete", True) or old.get("tries", 0) >= MAX_DETAIL_TRIES):
        return old
    try:
        got = sources.detail(listing["source"], listing["key"], listing["url"], listing["image"])
    except Exception as e:  # pylint: disable=broad-exception-caught
        log.warning("detail for listing %s failed: %s", listing["id"], type(e).__name__)
        return old
    files, missed = [], 0
    for url in got["images"]:
        try:
            files.append(full(images.fetch(url, listing["url"]), s.img_dir))
        except Exception as e:  # pylint: disable=broad-exception-caught
            missed += 1
            log.warning("detail photo failed: %s", type(e).__name__)
    cleared = old.get("photos", [])
    fresh = [f for f in dict.fromkeys(files) if f not in cleared]
    clear: bool | None = True
    if fresh:
        shots = [images.as_jpeg((s.img_dir / f).read_bytes(), 768) for f in fresh]
        clear = judge.gallery(db, model, listing["name"], [x for x in shots if x])
    if clear is False:
        db.run(
            "UPDATE listings SET dropped = 1, reason = 'hidden: a photo on its page',"
            " detail = '' WHERE id = ?",
            listing["id"],
        )
        return {"dropped": True}
    facts = judge.facts(db, model, got["text"])
    detail = {
        "photos": cleared + fresh if clear else cleared,
        "text": got["text"] or old.get("text", ""),
        "facts": facts or old.get("facts", {}),
        "complete": not missed and clear is True and facts is not None,
        "tries": old.get("tries", 0) + 1,
    }
    if detail["photos"] or detail["text"]:
        db.run("UPDATE listings SET detail = ? WHERE id = ?", json.dumps(detail), listing["id"])
    return detail


def judge_new(db: DB, s: Settings, model: LLM, prof: dict, new: list[collect.Item]) -> dict:
    """Sort this sweep's new listings. Returns the listing rows under ``picks`` and ``rest``,
    and under ``held`` the (shop, key) pairs to leave unseen: over a cap, or not answered by
    the model."""
    out: dict = {
        "picks": [],
        "rest": [],
        "held": [],
        "hidden": 0,
        "unanswered": 0,
        "over": 0,
    }
    out.update(answered=0, asked=0)
    for i in new:
        r = row(db, i)
        if profile.score(i.name, i.tags, i.url, prof)[0] < 0:
            record(db, r["id"], i, (True, 0, "hidden by a keyword"))
            out["hidden"] += 1
            continue
        if out["asked"] >= s.judge_cap:
            out["held"].append((i.source, i.key))
            out["over"] += 1
            continue
        out["asked"] += 1
        got, name = ask(db, s, model, prof, r, i)
        if got is None:
            out["held"].append((i.source, i.key))
            out["unanswered"] += 1
            continue
        dropped, liked, reason = got
        out["answered"] += 1
        record(db, r["id"], i, got)
        if dropped:
            out["hidden"] += 1
            continue
        r.update(dropped=0, liked=liked, reason=reason, photo=name)
        out["picks" if liked >= 2 else "rest"].append(r)
    out["picks"].sort(key=lambda r: -r["liked"])
    # One mail carries only so many. The rest stay new, so the next sweep's mail has them.
    for kind, most in (("picks", MAX_PICKS), ("rest", MAX_REST)):
        for r in out[kind][most:]:
            out["held"].append((r["source"], r["key"]))
            out["over"] += 1
        out[kind] = out[kind][:most]
    return out


def ask(db: DB, s: Settings, model: LLM, prof: dict, r: dict, i: collect.Item):
    """(the model's verdict or None, the stored photo's name). A listing that has a photo
    the app could not fetch gets no verdict: it would be one on the title alone."""
    name = photo(db, s, r)
    if r["image"] and not name:
        return None, ""
    jpeg = images.as_jpeg((s.img_dir / name).read_bytes(), 768) if name else None
    return judge.listing(db, model, prof, i.name, i.source, i.tags, jpeg), name


def record(db: DB, listing_id: int, i: collect.Item, got: tuple[bool, int, str]) -> None:
    db.run(
        "UPDATE listings SET dropped = ?, liked = ?, reason = ?, judged = ?, judged_as = ?"
        " WHERE id = ?",
        int(got[0]),
        got[1],
        got[2],
        runs.now(),
        sig(i.name, i.tags),
        listing_id,
    )


def recheck(
    db: DB, s: Settings, model: LLM, prof: dict, known: list[collect.Item], budget: int
) -> tuple[int, int]:
    """Judge again the listings whose title or tags changed since their verdict (``store``
    has already hidden them). A listing already dropped stays dropped. Returns how many the
    model answered for and how many it did not; one with no answer stays hidden and is asked
    about again next sweep."""
    answered = unanswered = 0
    for i in known:
        r = row(db, i)
        if not r or not r["judged_as"] or r["dropped"] or r["judged_as"] == sig(i.name, i.tags):
            continue
        if profile.score(i.name, i.tags, i.url, prof)[0] < 0:
            record(db, r["id"], i, (True, 0, "hidden by a keyword"))
            continue
        if budget <= 0:
            continue
        budget -= 1
        got, _ = ask(db, s, model, prof, r, i)
        if got is None:
            unanswered += 1
        else:
            answered += 1
            record(db, r["id"], i, got)
    return answered, unanswered


def notes_for(sorted_: dict) -> list[str]:
    out = []
    if sorted_["unanswered"]:
        out.append(
            f"The model did not answer for {sorted_['unanswered']} new listing(s);"
            " they will be tried again on the next sweep."
        )
    if sorted_["over"]:
        out.append(f"{sorted_['over']} more new listing(s) are held over to the next sweep.")
    return out


def render(s: Settings, picks: list, rest: list, cover: list, notes: list, subject: str):
    """(html, text, photos) for the mail and for its copy on the web."""
    c = s.common
    photos = mail.Photos(domain=c.name, limit=MAX_PICKS + MAX_REST)
    for r, side in [(r, 640) for r in picks] + [(r, 320) for r in rest]:
        if r["photo"]:
            photos.add(r["photo"], mail.thumb(s.img_dir / r["photo"], side))
    ctx = {
        "picks": picks,
        "rest": rest,
        "coverage": cover,
        "notes": notes,
        "title": subject,
    }
    ctx["link"] = c.link
    html = ENV.get_template("mail.html").render(src=photos.src, **ctx)
    web = ENV.get_template("mail.html").render(src=lambda k: f"/img/{k}" if k else "", **ctx)
    text = "\n".join(
        [subject, ""]
        + [f"{r['name']}  {r['price']}  {c.link('/listing/' + str(r['id']))}" for r in picks + rest]
        + [""]
        + notes
        + cover
    )
    return html, text, photos, web


def deliver(db: DB, s: Settings, run_id: int, sorted_: dict, send: bool) -> str:
    """Mail the picks and the rest; 'sent', 'off' or 'failed: <reason>'."""
    c = s.common
    picks, rest = sorted_["picks"], sorted_["rest"]
    day = today(c.timezone)
    subject = f"Toys: {len(picks)} for you, {len(rest)} other new ({day:%d %b})"
    html, text, photos, web = render(
        s, picks, rest, runs.coverage(db, run_id), notes_for(sorted_), subject
    )
    db.put(LAST_MAIL, {"subject": subject, "html": web, "at": runs.now(), "sent": False})
    if not send or not c.mail_on:
        return "off"
    result = mail.send(
        c.smtp_host,
        c.smtp_port,
        c.mail_from,
        c.mail_to,
        subject,
        html,
        text,
        photos,
        "Toys",
    )
    if result == "sent":
        db.put(LAST_MAIL, {"subject": subject, "html": web, "at": runs.now(), "sent": True})
        stamp = runs.now()
        for r in picks + rest:
            db.run("UPDATE listings SET mailed = ? WHERE id = ?", stamp, r["id"])
        if picks:
            path = c.link(f"/listing/{picks[0]['id']}")
            pushed = notify.send(c.apprise_urls, subject, picks[0]["name"], path, "Open")
            log.info("alert: %s", pushed)
    return result


def heartbeat(db: DB, s: Settings, run_id: int, notes: list[str]) -> str:
    """On a Monday with nothing to show, one short mail a week says the watch still runs."""
    c = s.common
    day = today(c.timezone)
    week = f"{day.isocalendar().year}-{day.isocalendar().week}"
    if not c.mail_on or day.weekday() != 0 or db.get(HEARTBEAT, "") == week:
        return "nothing new"
    subject = f"Toys: nothing new, still running ({day:%d %b})"
    cover = runs.coverage(db, run_id)
    html, text, photos, _ = render(s, [], [], cover, notes, subject)
    result = mail.send(
        c.smtp_host,
        c.smtp_port,
        c.mail_from,
        c.mail_to,
        subject,
        html,
        text,
        photos,
        "Toys",
    )
    if result == "sent":
        db.put(HEARTBEAT, week)
        return "nothing new"
    return result


def run(
    db: DB,
    s: Settings,
    trigger: str = "schedule",
    notify_on: bool = True,
    sample: int = 0,
) -> int:
    """Run one sweep; returns its run id, or 0 when another sweep is still going.

    ``sample`` treats the newest N listings of each shop as new and marks nothing seen: a
    preview of the judging and the mail, which is rendered but not sent."""
    if LOCK.locked():
        return 0
    with LOCK:
        return _run(db, s, trigger, notify_on and not sample, sample)


def _run(db: DB, s: Settings, trigger: str, notify_on: bool, sample: int) -> int:
    run_id = runs.start(db, trigger)
    try:
        c = s.common
        model = LLM(c.llm_url, c.llm_key, c.llm_model)
        new: list[collect.Item] = []
        known: list[collect.Item] = []
        read: dict[str, list[str]] = {}  # the keys each shop showed this sweep
        for got in collect.run(sources.jobs()):
            if got.items is None:  # a failed read is not an empty one
                runs.source(db, run_id, got.name, error=got.error, seconds=got.seconds)
                continue
            runs.source(db, run_id, got.name, count=len(got.items), seconds=got.seconds)
            store(db, got.items)
            fresh = set(seen.fresh(db, got.name, [i.key for i in got.items]))
            new += got.items[:sample] if sample else [i for i in got.items if i.key in fresh]
            known += [i for i in got.items if i.key not in fresh]
            read[got.name] = [i.key for i in got.items]
            if not sample and seen.baselined(db, got.name):
                owed = pending(db, got.name, set(read[got.name]))
                new += owed
                read[got.name] += [i.key for i in owed]
        prof = profile.load(db)
        sorted_ = judge_new(db, s, model, prof, new)
        again = (0, 0)
        if not sample:
            again = recheck(db, s, model, prof, known, s.judge_cap - sorted_["asked"])
        if sorted_["picks"] or sorted_["rest"]:
            result = deliver(db, s, run_id, sorted_, notify_on)
        elif (sorted_["unanswered"] or again[1]) and not (sorted_["answered"] or again[0]):
            result = "failed: the model did not answer"
        else:
            result = heartbeat(db, s, run_id, notes_for(sorted_)) if notify_on else "nothing new"
        held: dict[str, set[str]] = {}
        for source, key in sorted_["held"]:
            held.setdefault(source, set()).add(key)
        for name, keys in read.items():
            if sample:
                break
            if not seen.baselined(db, name):
                seen.commit(db, name, keys)
            elif result in ("sent", "nothing new") or result.startswith("failed: the model"):
                # What was mailed, hidden or already known is done with; what is held is not.
                seen.commit(db, name, [k for k in keys if k not in held.get(name, set())])
        log.info("sweep %s: %s, model calls %s", run_id, result, model.usage.calls)
        runs.finish(db, run_id, alerts=result)
    except Exception as e:  # pylint: disable=broad-exception-caught
        log.exception("sweep crashed")
        runs.finish(db, run_id, status="failed", error=type(e).__name__)
    return run_id
