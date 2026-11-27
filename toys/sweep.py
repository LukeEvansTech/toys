"""One sweep: read every shop, store what it showed, judge what is new, mail what is worth
showing.

The order matters. A listing is marked seen only after the mail that carried it went, so a
failed send leaves it new for the next sweep. A shop read for the first time is recorded at
once and announces nothing. A listing the model did not answer for is neither mailed nor
marked seen: it is asked about again next time.
"""

from __future__ import annotations

import datetime as dt
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
HEARTBEAT = "heartbeat_week"
LAST_MAIL = "last_mail"


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
                (i.source, i.key, i.name, i.url, i.price, i.image, json.dumps(i.tags), stamp, stamp)
                for i in items
            ],
        )


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
    """Fetch a listing's own page once: every photo at full size, the description, and the
    facts in it. Returns the detail, or {} when the shop could not be read (tried again on
    the next view)."""
    if listing["detail"]:
        return json.loads(listing["detail"])
    try:
        got = sources.detail(listing["source"], listing["key"], listing["url"])
    except Exception as e:  # pylint: disable=broad-exception-caught
        log.warning("detail for listing %s failed: %s", listing["id"], type(e).__name__)
        return {}
    photos = []
    for url in got["images"]:
        try:
            photos.append(images.store(images.fetch(url, listing["url"]), s.img_dir, 1600))
        except Exception as e:  # pylint: disable=broad-exception-caught
            log.warning("detail photo failed: %s", type(e).__name__)
    detail = {
        "photos": list(dict.fromkeys(photos)),
        "text": got["text"],
        "facts": judge.facts(db, model, got["text"]),
    }
    if detail["photos"] or detail["text"]:
        db.run("UPDATE listings SET detail = ? WHERE id = ?", json.dumps(detail), listing["id"])
    return detail


def judge_new(db: DB, s: Settings, model: LLM, prof: dict, new: list[collect.Item]) -> dict:
    """Sort this sweep's new listings. Returns the listing rows under ``picks`` and ``rest``,
    and under ``held`` the items to leave unseen: over the cap, or not answered by the model."""
    out: dict = {"picks": [], "rest": [], "held": [], "hidden": 0, "unanswered": 0, "over": 0}
    out["answered"] = 0
    asked = 0
    for i in new:
        r = row(db, i)
        if profile.score(i.name, i.tags, i.url, prof)[0] < 0:
            db.run(
                "UPDATE listings SET dropped = 1, liked = 0, reason = 'hidden by a keyword',"
                " judged = ? WHERE id = ?",
                runs.now(),
                r["id"],
            )
            out["hidden"] += 1
            continue
        if asked >= s.judge_cap:
            out["held"].append(i)
            out["over"] += 1
            continue
        asked += 1
        name = photo(db, s, r)
        jpeg = images.as_jpeg((s.img_dir / name).read_bytes(), 768) if name else None
        got = judge.listing(db, model, prof, i.name, i.source, i.tags, jpeg)
        if got is None:
            out["held"].append(i)
            out["unanswered"] += 1
            continue
        dropped, liked, reason = got
        out["answered"] += 1
        db.run(
            "UPDATE listings SET dropped = ?, liked = ?, reason = ?, judged = ? WHERE id = ?",
            int(dropped),
            liked,
            reason,
            runs.now(),
            r["id"],
        )
        if dropped:
            out["hidden"] += 1
            continue
        r.update(dropped=0, liked=liked, reason=reason, photo=name)
        out["picks" if liked >= 2 else "rest"].append(r)
    out["picks"].sort(key=lambda r: -r["liked"])
    return out


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
    ctx = {"picks": picks, "rest": rest, "coverage": cover, "notes": notes, "title": subject}
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
    picks, rest = sorted_["picks"][:MAX_PICKS], sorted_["rest"][:MAX_REST]
    day = dt.datetime.now(ZoneInfo(c.timezone))
    subject = f"Toys: {len(picks)} for you, {len(rest)} other new ({day:%d %b})"
    html, text, photos, web = render(
        s, picks, rest, runs.coverage(db, run_id), notes_for(sorted_), subject
    )
    db.put(LAST_MAIL, {"subject": subject, "html": web, "at": runs.now(), "sent": False})
    if not send or not c.mail_on:
        return "off"
    result = mail.send(
        c.smtp_host, c.smtp_port, c.mail_from, c.mail_to, subject, html, text, photos, "Toys"
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
    day = dt.datetime.now(ZoneInfo(c.timezone))
    week = f"{day.isocalendar().year}-{day.isocalendar().week}"
    if not c.mail_on or day.weekday() != 0 or db.get(HEARTBEAT, "") == week:
        return "nothing new"
    subject = f"Toys: nothing new, still running ({day:%d %b})"
    cover = runs.coverage(db, run_id)
    html, text, photos, _ = render(s, [], [], cover, notes, subject)
    result = mail.send(
        c.smtp_host, c.smtp_port, c.mail_from, c.mail_to, subject, html, text, photos, "Toys"
    )
    if result == "sent":
        db.put(HEARTBEAT, week)
        return "nothing new"
    return result


def run(
    db: DB, s: Settings, trigger: str = "schedule", notify_on: bool = True, sample: int = 0
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
        read: dict[str, list[str]] = {}  # the keys each shop showed this sweep
        for got in collect.run(sources.jobs()):
            if got.items is None:  # a failed read is not an empty one
                runs.source(db, run_id, got.name, error=got.error, seconds=got.seconds)
                continue
            runs.source(db, run_id, got.name, count=len(got.items), seconds=got.seconds)
            store(db, got.items)
            fresh = set(seen.fresh(db, got.name, [i.key for i in got.items]))
            new += got.items[:sample] if sample else [i for i in got.items if i.key in fresh]
            read[got.name] = [i.key for i in got.items]
        sorted_ = judge_new(db, s, model, profile.load(db), new)
        if sorted_["picks"] or sorted_["rest"]:
            result = deliver(db, s, run_id, sorted_, notify_on)
        elif sorted_["unanswered"] and not sorted_["answered"]:
            result = "failed: the model did not answer"
        else:
            result = heartbeat(db, s, run_id, notes_for(sorted_)) if notify_on else "nothing new"
        held: dict[str, set[str]] = {}
        for i in sorted_["held"]:
            held.setdefault(i.source, set()).add(i.key)
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
