"""What Luke collects, and the rules that run before the model is asked.

The profile is stored in the app's database (it names what he owns and likes, so it is not in
the repository). Rules are pure: a listing and a profile in, a verdict out.
"""

from __future__ import annotations

import re

from collectorkit.db import DB

KEY = "profile"
# Shops label some sexualised statues; these words hide a listing before any model call.
EXCLUDE = [
    "r18",
    "r-18",
    "r 18",
    "18+",
    "nsfw",
    "sexy",
    "cast off",
    "cast-off",
    "castoff",
    "nude",
    "naked",
    "lingerie",
    "bikini",
    "swimsuit",
    "bunny girl",
    "hentai",
    "ecchi",
    "succubus",
]
EMPTY: dict = {
    "franchises": {},  # name -> how many owned
    "studios": {},  # name -> how many owned
    "aliases": {},  # franchise -> words a shop uses for it
    "characters": {},  # franchise -> liked characters
    "never": [],
    "exclude": EXCLUDE,
    "notes": [],
}


def clean(raw: dict) -> dict:
    """A profile with every part present and of the right shape; anything else is dropped."""
    out: dict = {}
    for k, default in EMPTY.items():
        v = raw.get(k)
        if isinstance(default, dict):
            v = v if isinstance(v, dict) else {}
            if k in ("franchises", "studios"):
                out[k] = {str(a): int(b) for a, b in v.items() if isinstance(b, (int, float))}
            else:
                out[k] = {str(a): _words(b) for a, b in v.items() if isinstance(b, list)}
        else:
            out[k] = _words(v) if isinstance(v, list) else list(default)
    return out


def _words(v: list) -> list[str]:
    out: list[str] = []
    for x in v:
        x = str(x).strip()
        if x and x not in out:
            out.append(x[:200])
    return out


def load(db: DB) -> dict:
    return clean(db.get(KEY, {}))


def save(db: DB, prof: dict) -> None:
    db.put(KEY, clean(prof))


def lines(text: str) -> list[str]:
    """A form field with one entry per line, or separated by commas."""
    return _words(re.split(r"[\n,]", text or ""))


def has(word: str, text: str) -> bool:
    """True if word (3+ characters) appears in text as a whole word."""
    w = word.lower()
    return len(w) >= 3 and re.search(rf"(?<![a-z0-9]){re.escape(w)}(?![a-z0-9])", text) is not None


def score(name: str, tags: list[str], url: str, prof: dict) -> tuple[int, list[str]]:
    """(score, reasons). -1 hides the listing. A liked character outranks a franchise-only
    match; a franchise with a character list scores low when none of them is named."""
    t = " ".join([name, " ".join(tags), url]).lower()
    if any(has(x, t) for x in prof["exclude"]) or any(has(n, t) for n in prof["never"]):
        return -1, []
    s, why = 0, []
    for fr, n in prof["franchises"].items():
        chars = prof["characters"].get(fr, [])
        hit = [c for c in chars if has(c, t)]
        if hit or any(has(a, t) for a in prof["aliases"].get(fr, [fr])):
            if hit:
                s += 30 + n
                why.append(f"{hit[0]} · {fr} ({n} owned)")
            elif not chars:
                s += 10 + n
                why.append(f"{fr} ({n} owned)")
            else:
                s += 2
                why.append(f"{fr}, not a character on your list")
            break
    for st, n in prof["studios"].items():
        if has(st, t):
            s += 5 + n
            why.append(f"{st} ({n} owned)")
            break
    return s, why
