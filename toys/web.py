"""Pages. Sign-in is the cluster's job (the route sits behind Pocket ID); /img is served on a
second, open route so a mail client can load photos without a session."""

from __future__ import annotations

import datetime as dt
import json
import threading
from pathlib import Path

from collectorkit import metrics, runs, scheduler
from collectorkit.db import DB
from collectorkit.llm import LLM
from fastapi import FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import collection, profile, sweep
from .config import Settings
from .db import open_db

HERE = Path(__file__).parent
MAX_UPLOAD = 5_000_000
FACT_LABELS = {
    "scale": "Scale",
    "size": "Size",
    "edition": "Edition",
    "material": "Material",
    "studio": "Studio",
    "deposit": "Deposit",
    "balance": "Balance",
    "release": "Release",
}


def ago(stamp: str | None) -> str:
    if not stamp:
        return "never"
    mins = int((dt.datetime.now(dt.timezone.utc) - runs.parse(stamp)).total_seconds() // 60)
    if mins < 1:
        return "just now"
    if mins < 60:
        return f"{mins} min ago"
    if mins < 60 * 36:
        return f"{mins // 60} h ago"
    return f"{mins // 1440} days ago"


def gbp(value: float | None) -> str:
    if value is None:
        return "–"
    return f"£{value:,.0f}" if value == int(value) else f"£{value:,.2f}"


def create_app(s: Settings, opened: DB | None = None, start_scheduler: bool = False) -> FastAPI:
    db = opened or open_db(s.common.db_path)
    app = FastAPI(title="toys", docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    tpl = Jinja2Templates(directory=HERE / "templates")
    tpl.env.filters.update(ago=ago, gbp=gbp)

    def page(request: Request, name: str, nav: str, **ctx):
        base = {
            "nav": nav,
            "version": s.common.version,
            "running": sweep.LOCK.locked(),
            "last_run": db.one("SELECT * FROM runs WHERE finished IS NOT NULL ORDER BY id DESC"),
            "open_count": len(collection.find(db, show="open")),
        }
        return tpl.TemplateResponse(request, name, base | ctx)

    @app.get("/", response_class=HTMLResponse)
    def shelf(request: Request, show: str = "", franchise: str = "", vendor: str = "", q: str = ""):
        everything = collection.find(db)
        items = collection.find(db, show, franchise, vendor, q.strip())
        return page(
            request,
            "shelf.html",
            "shelf",
            items=items,
            total=sum(i["gbp_paid"] or 0 for i in items),
            unpriced=sum(1 for i in items if i["gbp_paid"] is None),
            f={"show": show, "franchise": franchise, "vendor": vendor, "q": q},
            groups=collection.GROUPS,
            franchises=sorted({i["franchise"] for i in everything if i["franchise"]}),
            vendors=sorted({i["vendor"] for i in everything if i["vendor"]}),
            empty=not everything,
        )

    @app.get("/orders", response_class=HTMLResponse)
    def orders(request: Request):
        items = collection.find(db, show="open")
        by_state: dict[str, list[dict]] = {}
        for i in sorted(items, key=lambda i: (i["state"], i["vendor"], i["bought"])):
            by_state.setdefault(i["state"], []).append(i)
        return page(
            request,
            "orders.html",
            "orders",
            by_state=by_state,
            count=len(items),
            total=sum(i["gbp_paid"] or 0 for i in items),
        )

    @app.get("/item/{item_id}", response_class=HTMLResponse)
    def item(request: Request, item_id: str):
        found = [i for i in collection.find(db) if i["id"] == item_id]
        if not found:
            raise HTTPException(404)
        return page(request, "item.html", "shelf", i=found[0])

    @app.get("/new", response_class=HTMLResponse)
    def new(request: Request):
        shown = db.all(
            "SELECT * FROM listings WHERE judged IS NOT NULL AND dropped = 0"
            " ORDER BY first_seen DESC, liked DESC, name LIMIT 300"
        )
        return page(
            request,
            "new.html",
            "new",
            picks=[r for r in shown if (r["liked"] or 0) >= 2],
            rest=[r for r in shown if (r["liked"] or 0) < 2],
            waiting=db.one("SELECT COUNT(*) n FROM listings WHERE judged IS NULL")["n"],
        )

    @app.get("/listing/{listing_id}", response_class=HTMLResponse)
    def listing(request: Request, listing_id: int):
        r = db.one("SELECT * FROM listings WHERE id = ?", listing_id)
        # An unjudged or dropped listing has no page: its photos have not been cleared.
        if r is None or r["judged"] is None or r["dropped"]:
            raise HTTPException(404)
        c = s.common
        detail = sweep.fill_detail(db, s, LLM(c.llm_url, c.llm_key, c.llm_model), dict(r))
        if detail.get("dropped"):  # a photo on its own page was not clear
            raise HTTPException(404)
        photos = detail.get("photos") or ([r["photo"]] if r["photo"] else [])
        facts = [(FACT_LABELS[k], v) for k, v in (detail.get("facts") or {}).items()]
        return page(
            request,
            "listing.html",
            "new",
            r=r,
            photos=photos,
            facts=facts,
            text=detail.get("text", ""),
            tags=json.loads(r["tags"]),
        )

    @app.get("/profile", response_class=HTMLResponse)
    def profile_page(request: Request, saved: str = ""):
        prof = profile.load(db)
        owned = collection.owned_counts(db)
        names = list(dict.fromkeys(list(prof["franchises"]) + list(prof["characters"])))
        return page(
            request, "profile.html", "profile", p=prof, owned=owned, names=names, saved=saved
        )

    @app.post("/profile")
    async def profile_save(request: Request):
        form = await request.form()
        prof = profile.load(db)
        prof["never"] = profile.lines(str(form.get("never", "")))
        prof["notes"] = [x.strip() for x in str(form.get("notes", "")).splitlines() if x.strip()]
        chars = {}
        for name, value in form.multi_items():
            if name.startswith("chars:") and isinstance(value, str):
                chars[name[6:]] = profile.lines(value)
        added = str(form.get("add_franchise", "")).strip()
        if added and added not in chars:
            chars[added] = profile.lines(str(form.get("add_characters", "")))
            prof["franchises"].setdefault(added, 0)
        prof["characters"] = chars
        profile.save(db, prof)
        return RedirectResponse("/profile?saved=1", status_code=303)

    @app.get("/log", response_class=HTMLResponse)
    def log_page(request: Request):
        recent = db.all("SELECT * FROM runs ORDER BY id DESC LIMIT 30")
        return page(
            request,
            "log.html",
            "log",
            recent=[(r, runs.coverage(db, r["id"])) for r in recent],
            next_at=scheduler.next_time(s.schedule, s.common.timezone),
            last_mail=sweep.last_mail(db),
            counts={
                "collection": db.one("SELECT COUNT(*) n FROM collection")["n"],
                "listings": db.one("SELECT COUNT(*) n FROM listings")["n"],
            },
        )

    @app.get("/mail", response_class=HTMLResponse)
    def last_mail():
        m = sweep.last_mail(db)
        if not m:
            raise HTTPException(404)
        return HTMLResponse(m["html"])

    @app.post("/import")
    async def import_data(kind: str = Form(...), file: UploadFile | None = None):
        if file is None:
            raise HTTPException(400, "no file")
        raw = await file.read(MAX_UPLOAD + 1)
        if len(raw) > MAX_UPLOAD:
            raise HTTPException(413, "file too large")
        try:
            data = json.loads(raw)
            if kind == "collection":
                collection.replace(db, data)
            elif kind == "profile" and isinstance(data, dict):
                profile.save(db, data)
            else:
                raise ValueError("unknown kind")
        except ValueError as e:  # JSONDecodeError is one
            raise HTTPException(400, f"not loaded: {type(e).__name__}") from e
        return RedirectResponse("/log", status_code=303)

    @app.post("/sweep")
    def sweep_now():
        threading.Thread(target=sweep.run, args=(db, s, "manual"), daemon=True).start()
        return RedirectResponse("/log", status_code=303)

    @app.get("/img/{name}")
    def img(name: str):
        path = s.img_dir / name
        if "/" in name or ".." in name or not path.is_file():
            raise HTTPException(404)
        return FileResponse(path, media_type="image/jpeg")

    # The alert reads this gauge: a sweep counts only when it read listings, the model
    # answered, and its mail neither failed nor was switched off with listings waiting.
    delivered = runs.DELIVERED + " AND alerts NOT LIKE 'failed%' AND alerts != 'off'"
    metrics.mount(
        app,
        "toys",
        s.common.version,
        lambda: runs.last_success(db, delivered),
        lambda: {
            "listings": db.one("SELECT COUNT(*) n FROM listings")["n"],
            "unjudged_listings": db.one(
                "SELECT COUNT(*) n FROM listings l JOIN seen_sources b ON b.source = l.source"
                " WHERE l.judged IS NULL AND NOT EXISTS (SELECT 1 FROM seen x"
                " WHERE x.source = l.source AND x.key = l.key)"
            )["n"],
        },
    )

    if start_scheduler:
        scheduler.start(s.schedule, s.common.timezone, lambda: sweep.run(db, s), "sweep")
        if db.one("SELECT 1 FROM runs LIMIT 1") is None:
            # A fresh volume: take the baseline now, so the last-success gauge is set well
            # before the stale alert's wait runs out, not at the first scheduled time.
            threading.Thread(target=sweep.run, args=(db, s, "first"), daemon=True).start()
    return app
