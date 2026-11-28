"""What Luke owns and has on order. The records are built outside the app and loaded as a
whole; the app's database is the only copy the cluster holds."""

from __future__ import annotations

import json
import math

from collectorkit.db import DB

# The dataset's states, grouped the way the pages show them.
GROUPS = {
    "open": "On order",
    "have": "On the shelf",
    "closed": "Cancelled or refunded",
}


def group(state: str) -> str:
    s = state.lower()
    if s.startswith(("chase", "check", "waitlist")):
        return "open"
    if s.startswith(("received", "assumed")):
        return "have"
    return "closed"


def _num(v) -> float | None:
    try:
        n = None if v is None or v == "" else float(v)
    except (TypeError, ValueError):
        return None
    return n if n is not None and math.isfinite(n) else None  # "Infinity" is not a price


def _text(v) -> str:
    return "" if v is None else str(v).strip()


def rows(records: list) -> list[tuple]:
    """Rows for the collection table. Raises ValueError when the file is not the dataset."""
    if not isinstance(records, list) or not records:
        raise ValueError("expected a list of records")
    out, ids = [], set()
    for r in records:
        if not isinstance(r, dict) or not _text(r.get("id")) or not _text(r.get("item")):
            raise ValueError("a record has no id or no item")
        if r["id"] in ids:
            raise ValueError(f"id {r['id']} appears twice")
        ids.add(r["id"])
        if _text(r.get("state")) == "merged":  # folded into another record
            continue
        out.append(
            (
                _text(r["id"]),
                _text(r["item"]),
                _text(r.get("maker")),
                _text(r.get("vendor")),
                _text(r.get("order_ref")),
                _text(r.get("date")),
                _num(r.get("gbp_paid")),
                _text(r.get("currency")),
                _num(r.get("amount")),
                _text(r.get("seller_status")),
                _text(r.get("franchise")),
                _text(r.get("state")),
                _text(r.get("type")),
                int(bool(r.get("delivered_evidence"))),
                json.dumps(r.get("notes") or [], ensure_ascii=False),
                json.dumps(r.get("parcels") or [], ensure_ascii=False),
            )
        )
    return out


def replace(db: DB, records: list) -> int:
    """Swap the whole collection for ``records`` in one transaction; returns how many."""
    new = rows(records)
    with db.tx() as c:
        c.execute("DELETE FROM collection")
        c.executemany(
            "INSERT INTO collection (id, item, maker, vendor, order_ref, bought, gbp_paid,"
            " currency, amount, seller_status, franchise, state, kind, delivered, notes, parcels)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            new,
        )
    return len(new)


def photo_links(db: DB, links: dict) -> int:
    """Record where each piece's photo is to be fetched from; returns how many changed. A
    piece whose link is unchanged keeps the photo it has."""
    if not isinstance(links, dict) or not all(
        isinstance(k, str) and isinstance(v, str) and v.startswith("https://")
        for k, v in links.items()
    ):
        raise ValueError("expected record ids mapped to https links")
    changed = 0
    for k, v in links.items():
        old = db.one("SELECT url FROM piece_photos WHERE id = ?", k)
        if old is None or old["url"] != v:
            db.run(
                "INSERT INTO piece_photos (id, url, photo) VALUES (?, ?, '')"
                " ON CONFLICT(id) DO UPDATE SET url = excluded.url, photo = ''",
                k,
                v,
            )
            changed += 1
    return changed


def set_photo(db: DB, piece: str, name: str) -> None:
    db.run(
        "INSERT INTO piece_photos (id, photo) VALUES (?, ?)"
        " ON CONFLICT(id) DO UPDATE SET photo = excluded.photo",
        piece,
        name,
    )


def owned_counts(db: DB) -> dict[str, int]:
    """Pieces per franchise among what is owned or on order."""
    out: dict[str, int] = {}
    for r in db.all("SELECT franchise, state FROM collection WHERE franchise != ''"):
        if group(r["state"]) != "closed":
            out[r["franchise"]] = out.get(r["franchise"], 0) + 1
    return dict(sorted(out.items(), key=lambda kv: (-kv[1], kv[0])))


def find(db: DB, show: str = "", franchise: str = "", vendor: str = "", q: str = "") -> list[dict]:
    """Records for a page, newest purchase first, undated last."""
    out = []
    for r in db.all(
        "SELECT c.*, COALESCE(p.photo, '') photo FROM collection c LEFT JOIN piece_photos p"
        " ON p.id = c.id ORDER BY c.bought = '', c.bought DESC, c.item"
    ):
        d = dict(r)
        d["group"] = group(d["state"])
        if show and d["group"] != show:
            continue
        if franchise and d["franchise"] != ("" if franchise == "-" else franchise):
            continue
        if vendor and d["vendor"] != vendor:
            continue
        words = " ".join([d["item"], d["maker"], d["vendor"], d["order_ref"], d["id"]]).lower()
        if q and not all(w in words for w in q.lower().split()):
            continue
        d["notes"] = json.loads(d["notes"])
        d["parcels"] = json.loads(d["parcels"])
        out.append(d)
    return out
