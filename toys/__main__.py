"""toys command line.

python -m toys serve                         # web UI on :8080 plus the scheduled sweeps
python -m toys sweep [--no-notify]           # one sweep now
python -m toys sweep --sample N              # judge the N newest per shop; no mail, nothing marked
python -m toys backlog [--cap N] [--no-notify]  # judge what a baseline stored; mail the picks
python -m toys load collection|profile FILE  # replace the collection or the likes from JSON
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from . import collection, config, profile, sweep
from .db import open_db


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="toys")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve")
    sw = sub.add_parser("sweep")
    sw.add_argument("--no-notify", action="store_true")
    sw.add_argument("--sample", type=int, default=0)
    bl = sub.add_parser("backlog")
    bl.add_argument("--cap", type=int, default=0)
    bl.add_argument("--no-notify", action="store_true")
    ld = sub.add_parser("load")
    ld.add_argument("kind", choices=["collection", "profile"])
    ld.add_argument("file", type=Path)
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    s = config.load()
    db = open_db(s.common.db_path)

    if a.cmd == "serve":
        import uvicorn  # pylint: disable=import-outside-toplevel

        from .web import create_app  # pylint: disable=import-outside-toplevel

        app = create_app(s, db, start_scheduler=True)
        uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))  # nosec B104
        return 0
    if a.cmd == "load":
        data = json.loads(a.file.read_text(encoding="utf-8"))
        if a.kind == "collection":
            print(f"{collection.replace(db, data)} pieces loaded")
        else:
            profile.save(db, data)
            print("likes loaded")
        return 0
    if a.cmd == "backlog":
        done = sweep.backlog(db, s, a.cap or s.judge_cap, notify_on=not a.no_notify)
        print(done)
        return 1 if "failed" in done else 0
    run_id = sweep.run(db, s, trigger="cli", notify_on=not a.no_notify, sample=a.sample)
    row = db.one("SELECT status, alerts FROM runs WHERE id = ?", run_id)
    if row is None or row["status"] not in ("success", "partial"):
        return 1
    return 1 if row["alerts"].startswith("failed") else 0


if __name__ == "__main__":
    sys.exit(main())
