"""The shops this app reads, and how to read one product's own page.

A shop with a product feed is read from the feed; a page is rendered only when there is none.
Shops behind a bot check are not listed: they are not worked around.
"""

from __future__ import annotations

import html
import json
import os
import re
import subprocess  # nosec B404
from dataclasses import dataclass
from typing import Callable

from collectorkit import collect, images

# Say who is asking. A shop that refuses a client pretending to be a browser answers one that
# names itself, and a shop that refuses this one is not read at all.
UA = "toys/1 (+https://github.com/LukeEvansTech/toys)"
collect.UA = UA
images.UA = UA

Job = tuple[str, Callable[[], list[collect.Item]]]


@dataclass(frozen=True)
class Shop:
    name: str
    kind: str  # "woo" or "page"
    url: str
    href_match: str = ""
    img_match: str = ""
    img_sub: tuple[str, str] | None = None
    # Which photos on a product's own page are the product; ``img_match`` when not given.
    # ``{slug}`` stands for the last part of the product's link (its code, at some shops).
    detail_match: str = ""


SHOPS = [
    Shop("FavorGK", "woo", "https://www.favorgk.com"),
    Shop("orzGK", "woo", "https://orzgk.com"),
    Shop("Kaioland", "woo", "https://www.kaioland.com"),
    Shop(
        "FNC (FanaticAnimeStore)",
        "page",
        "https://www.fanaticanimestore.com/shop-all/?sort=newest",
        img_match=r"/products/",
        img_sub=(r"/stencil/\d+w/", "/stencil/1280x1280/"),
    ),
    Shop("Tsume", "page", "https://tsume-art.com/en/shop", href_match=r"/en/shop/[a-z0-9-]+-\d+$"),
    Shop(
        "Prime 1 Studio",
        "page",
        "https://www.prime1studio.com/series?prefn1=tagForFilter&prefv1=New",
        href_match=r"/[A-Z0-9-]+\.html$",
        img_match=r"/images/",
        detail_match=r"master-catalog/.+/images/{slug}/",
    ),
]
BY_NAME = {s.name: s for s in SHOPS}

# Every photo on a product page with its link, and the page's own words.
DETAIL_JS = """JSON.stringify({images:[...document.querySelectorAll("img")].map(i=>
i.currentSrc||i.src||i.dataset.src||"").filter(Boolean),
og:(document.querySelector('meta[property="og:image"]')||{}).content||"",
text:(document.body.innerText||"").replace(/\\s+/g," ").slice(0,6000)})"""


def _read(shop: Shop) -> list[collect.Item]:
    if shop.kind == "woo":
        return collect.woo(shop.url)
    return collect.page(shop.url, shop.href_match, shop.img_match, shop.img_sub)


def jobs() -> list[Job]:
    return [(s.name, lambda s=s: _read(s)) for s in SHOPS]  # type: ignore[misc]


def plain(markup: str) -> str:
    """A shop's description as plain text. It is someone else's markup, so none of it is kept."""
    text = re.sub(r"<(script|style)\b.*?</\1>", " ", markup or "", flags=re.S | re.I)
    text = re.sub(r"<br\s*/?>|</(p|div|li|h\d|tr)>", "\n", text, flags=re.I)
    text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    lines = [re.sub(r"[ \t\xa0]+", " ", x).strip() for x in text.splitlines()]
    return "\n".join(x for x in lines if x)[:6000]


def _web(urls: list[str], limit: int = 12) -> list[str]:
    out: list[str] = []
    for u in urls:
        if u.lower().startswith(("https://", "http://")) and u not in out:
            out.append(u)
    return out[:limit]


def woo_detail(product: dict) -> dict:
    words = (product.get("short_description") or "") + "\n" + (product.get("description") or "")
    return {
        "images": _web([i.get("src", "") for i in product.get("images") or []]),
        "text": plain(words),
    }


def page_detail(data: dict, shop: Shop, url: str = "") -> dict:
    images = [u for u in data.get("images") or [] if isinstance(u, str)]
    slug = re.sub(r"\.html?$", "", url.split("?")[0].rstrip("/").rsplit("/", 1)[-1])
    match = (shop.detail_match or shop.img_match).replace("{slug}", re.escape(slug))
    if match:
        images = [u for u in images if re.search(match, u)]
    else:
        images = []
    if shop.img_sub:
        images = [re.sub(shop.img_sub[0], shop.img_sub[1], u) for u in images]
    og = data.get("og") if isinstance(data.get("og"), str) else ""
    return {"images": _web([og] + images if og else images), "text": str(data.get("text", ""))}


def detail(source: str, key: str, url: str) -> dict:
    """Every photo and the description of one listing: ``{"images": [...], "text": "..."}``.
    Raises when the shop cannot be read; the caller keeps the listing as it was."""
    shop = BY_NAME.get(source)
    if shop is None:
        raise collect.CollectError("shop no longer read")
    if shop.kind == "woo":
        body = collect.http_get(f"{shop.url}/wp-json/wc/store/v1/products/{int(key)}")
        return woo_detail(json.loads(body))
    for attempt in (1, 2):  # a rendered read fails now and then; once more is enough
        r = subprocess.run(  # nosec B603
            [os.environ.get("OBSCURA", "obscura"), "scrape", url, "-e", DETAIL_JS, "-q"]
            + ["--timeout", "60"],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        try:
            return page_detail(json.loads(json.loads(r.stdout)["results"][0]["eval"]), shop, url)
        except (KeyError, IndexError, TypeError, ValueError):
            if attempt == 2:
                raise collect.CollectError("the product page could not be read") from None
    return {}
