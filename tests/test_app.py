"""The behaviours every collector keeps, and this app's own: with the shops, the model and the
mail stubbed. Every name and number here is made up."""

import datetime as dt
import io
import json

import pytest
from collectorkit import collect, runs
from fastapi.testclient import TestClient
from toys import collection, config, judge, profile, sources, sweep
from toys.db import open_db
from toys.web import create_app

PROFILE = {
    "franchises": {"Monsters": 3},
    "studios": {"Good Studio": 2},
    "aliases": {"Monsters": ["monsters", "pocket monsters"]},
    "characters": {"Monsters": ["Turtle", "Dragon"]},
    "never": ["Villain"],
    "notes": ["Turtle is a favourite."],
}


def item(key, name=None):
    return collect.Item(key, name or f"Item {key}", f"https://shop.example/{key}", "£10.00")


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("TOYS_DATA", str(tmp_path))
    monkeypatch.setenv("TOYS_PUBLIC_URL", "https://toys.example.test")
    monkeypatch.setenv("TOYS_SMTP_HOST", "smtp.example.test")
    monkeypatch.setenv("TOYS_MAIL_FROM", "noreply@example.test")
    monkeypatch.setenv("TOYS_MAIL_TO", "me@example.test")
    s = config.load()
    # "model": a verdict per listing name, or None for no answer
    state = {"shop": ["1", "2"], "mail": "sent", "sent": [], "model": {}, "asked": []}

    def jobs():
        def read():
            if state["shop"] is None:
                raise TimeoutError("shop down")
            return [k if isinstance(k, collect.Item) else item(k) for k in state["shop"]]

        return [("Shop", read)]

    def send(*args):
        if state["mail"] == "sent":
            state["sent"].append(args[4])  # the subject
        return state["mail"]

    def listing(_db, _model, _prof, name, *_):
        state["asked"].append(name)
        return state["model"].get(name, (False, 2, "a character he collects"))

    # A Wednesday, so the Monday heartbeat stays out of every test but its own.
    state["today"] = dt.datetime(2026, 1, 7, 7, 0, tzinfo=dt.UTC)
    monkeypatch.setattr(sweep, "today", lambda _tz: state["today"])
    monkeypatch.setattr(sweep.sources, "jobs", jobs)
    monkeypatch.setattr(sweep.mail, "send", send)
    monkeypatch.setattr(sweep.notify, "send", lambda *a, **k: "off")
    monkeypatch.setattr(sweep.images, "download", lambda *a, **k: "")
    monkeypatch.setattr(sweep.judge, "listing", listing)
    db = open_db(s.common.db_path)
    profile.save(db, PROFILE)
    return s, db, state


def subjects(state):
    return [x.split(" (")[0] for x in state["sent"]]


def test_the_first_sweep_is_a_baseline_and_new_listings_are_mailed_once(env):
    s, db, state = env
    sweep.run(db, s)
    assert state["sent"] == [] and state["asked"] == []
    state["shop"] = ["1", "2", "3", "3"]  # a feed can show one product twice
    sweep.run(db, s)
    sweep.run(db, s)
    assert subjects(state) == ["Toys: 1 for you, 0 other new"]
    assert db.one("SELECT mailed FROM listings WHERE key = '3'")["mailed"]


def test_the_backlog_judges_a_baseline_and_mails_only_its_picks(env):
    s, db, state = env
    state["model"] = {"Item 2": (False, 0, "not a franchise he collects")}
    sweep.run(db, s)
    assert sweep.backlog(db, s, 80) == "backlog: 2 judged, 0 left, mail sent"
    assert subjects(state) == ["Toys: 1 for you, 0 other new"]
    assert db.one("SELECT mailed FROM listings WHERE key = '1'")["mailed"]
    assert db.one("SELECT judged, mailed FROM listings WHERE key = '2'")["mailed"] is None
    assert sweep.backlog(db, s, 80) == "backlog: 0 judged, 0 left, mail none"
    sweep.run(db, s)
    assert len(state["sent"]) == 1 and sorted(state["asked"]) == ["Item 1", "Item 2"]


def test_a_backlog_pick_hidden_by_a_keyword_since_is_not_mailed(env):
    s, db, state = env
    state["shop"] = [item("1", "Dragon statue")]
    sweep.run(db, s)
    state["mail"] = "failed: SMTPConnectError"
    assert sweep.backlog(db, s, 80).endswith("mail failed: SMTPConnectError")
    profile.save(db, PROFILE | {"exclude": ["dragon"]})
    state["mail"] = "sent"
    assert sweep.backlog(db, s, 80) == "backlog: 0 judged, 0 left, mail none"
    assert state["sent"] == [] and db.one("SELECT dropped FROM listings")["dropped"] == 1


def test_a_failed_send_leaves_the_listing_new(env):
    s, db, state = env
    sweep.run(db, s)
    state["shop"], state["mail"] = ["1", "2", "3"], "failed: SMTPConnectError"
    run_id = sweep.run(db, s)
    assert db.one("SELECT alerts FROM runs WHERE id = ?", run_id)["alerts"].startswith("failed")
    state["mail"] = "sent"
    sweep.run(db, s)
    assert subjects(state) == ["Toys: 1 for you, 0 other new"]


def test_a_failed_source_is_not_a_success_and_retires_nothing(env):
    s, db, state = env
    sweep.run(db, s)
    before = runs.last_success(db)
    state["shop"] = None
    run_id = sweep.run(db, s)
    assert db.one("SELECT status FROM runs WHERE id = ?", run_id)["status"] == "failed"
    assert runs.coverage(db, run_id) == ["Shop: FAILED (TimeoutError)"]
    assert runs.last_success(db) == before
    assert db.one("SELECT COUNT(*) n FROM listings")["n"] == 2


def test_a_dropped_or_keyword_hidden_listing_is_never_mailed_or_shown(env):
    s, db, state = env
    sweep.run(db, s)
    state["shop"] = ["1", "2", item("3", "Lady statue"), item("4", "Hero NSFW ver.")]
    state["model"] = {"Lady statue": (True, 0, "")}
    sweep.run(db, s)
    assert state["sent"] == []  # nothing left to show: a silent day
    assert state["asked"] == ["Lady statue"]  # the keyword hid the other before any model call
    client = TestClient(create_app(s, db))
    page = client.get("/new").text
    assert "Lady statue" not in page and "Hero NSFW" not in page
    hidden = db.one("SELECT id FROM listings WHERE key = '3'")["id"]
    assert client.get(f"/listing/{hidden}").status_code == 404
    sweep.run(db, s)
    assert state["asked"] == ["Lady statue"]  # done with: not asked about again


def test_a_listing_the_model_did_not_answer_for_is_held_and_asked_again(env):
    s, db, state = env
    sweep.run(db, s)
    state["shop"] = ["1", "2", item("3", "Turtle"), item("4", "Mystery")]
    state["model"] = {"Mystery": None}
    sweep.run(db, s)
    assert subjects(state) == ["Toys: 1 for you, 0 other new"]
    client = TestClient(create_app(s, db))
    assert "Mystery" not in client.get("/new").text and "Mystery" not in client.get("/mail").text
    assert "toys_unjudged_listings 1" in client.get("/metrics").text
    state["model"] = {}
    sweep.run(db, s)
    assert subjects(state)[1:] == ["Toys: 1 for you, 0 other new"]
    assert state["asked"].count("Turtle") == 1 and state["asked"].count("Mystery") == 2


def test_a_model_that_answers_nothing_is_a_failed_delivery(env):
    s, db, state = env
    sweep.run(db, s)
    ok = runs.last_success(db, runs.DELIVERED + " AND alerts NOT LIKE 'failed%'")
    state["shop"] = ["1", "2", item("3", "Turtle")]
    state["model"] = {"Turtle": None}
    run_id = sweep.run(db, s)
    assert db.one("SELECT alerts FROM runs WHERE id = ?", run_id)["alerts"].startswith("failed")
    assert runs.last_success(db, runs.DELIVERED + " AND alerts NOT LIKE 'failed%'") == ok


def test_listings_over_the_cap_wait_for_the_next_sweep(env, monkeypatch):
    s, db, state = env
    monkeypatch.setenv("TOYS_JUDGE_CAP", "1")
    s = config.load()
    sweep.run(db, s)
    state["shop"] = ["1", "2", "3", "4"]
    sweep.run(db, s)
    sweep.run(db, s)
    assert subjects(state) == ["Toys: 1 for you, 0 other new"] * 2
    assert sorted(state["asked"]) == ["Item 3", "Item 4"]


def test_a_mail_carries_only_so_many_and_the_rest_follow(env, monkeypatch):
    s, db, state = env
    monkeypatch.setattr(sweep, "MAX_PICKS", 2)
    sweep.run(db, s)
    state["shop"] = ["1", "2", "3", "4", "5"]
    sweep.run(db, s)
    sweep.run(db, s)
    sweep.run(db, s)
    assert subjects(state) == [
        "Toys: 2 for you, 0 other new",
        "Toys: 1 for you, 0 other new",
    ]
    assert db.one("SELECT COUNT(*) n FROM listings WHERE mailed IS NOT NULL")["n"] == 3


def test_a_quiet_monday_sends_one_heartbeat_a_week(env):
    s, db, state = env
    state["today"] = dt.datetime(2026, 1, 5, 7, 0, tzinfo=dt.UTC)  # a Monday
    sweep.run(db, s)
    sweep.run(db, s)
    state["today"] += dt.timedelta(days=7)
    sweep.run(db, s)
    assert subjects(state) == ["Toys: nothing new, still running"] * 2


def test_a_renamed_listing_is_judged_again_before_it_is_shown(env):
    s, db, state = env
    sweep.run(db, s)
    state["shop"] = ["1", "2", item("3", "Turtle")]
    sweep.run(db, s)
    client = TestClient(create_app(s, db))
    assert "Turtle" in client.get("/new").text
    # the shop renames it: a keyword hides it without a model call
    state["shop"] = ["1", "2", item("3", "Turtle NSFW ver.")]
    sweep.run(db, s)
    assert "Turtle" not in client.get("/new").text
    # a rename the keywords do not catch is the model's to judge; no answer, not shown
    state["shop"] = ["1", "2", item("4", "Dragon")]
    sweep.run(db, s)
    state["shop"] = ["1", "2", item("4", "Dragon in a new pose")]
    state["model"] = {"Dragon in a new pose": None}
    run_id = sweep.run(db, s)
    assert "Dragon" not in client.get("/new").text
    assert db.one("SELECT alerts FROM runs WHERE id = ?", run_id)["alerts"].startswith("failed")
    assert client.get("/mail").status_code == 404  # the saved mail shows it as it was
    # the shop stops showing it before the model answers: it is still asked about
    state["shop"] = ["1", "2"]
    state["model"] = {"Dragon in a new pose": (True, 0, "")}
    sweep.run(db, s)
    assert "Dragon" not in client.get("/new").text
    assert db.one("SELECT dropped FROM listings WHERE key = '4'")["dropped"] == 1
    assert len(state["sent"]) == 2  # each was mailed once, when new


def test_a_sample_marks_nothing_and_sends_nothing(env):
    s, db, state = env
    sweep.run(db, s, sample=1)
    assert state["sent"] == [] and state["asked"] == ["Item 1"]
    assert db.get(sweep.LAST_MAIL, None)["sent"] is False
    sweep.run(db, s)  # still the baseline
    assert state["sent"] == []
    state["shop"] = ["2"]  # the sampled listing has gone from the shop's page
    sweep.run(db, s)
    assert state["sent"] == []  # and is not announced later as if it were new


def test_the_rules_score_characters_not_franchises():
    p = profile.clean(PROFILE)
    assert profile.score("Monsters Turtle 1/6", [], "", p)[0] > 30
    assert profile.score("Monsters Bird 1/6", [], "", p) == (
        2,
        ["Monsters, not a character on your list"],
    )
    assert profile.score("Monsters Villain", [], "", p)[0] == -1
    assert profile.score("Turtle (sexy ver.)", [], "", p)[0] == -1
    assert profile.score("Unrelated robot", ["Good Studio"], "", p) == (
        7,
        ["Good Studio (2 owned)"],
    )
    assert profile.lines("Turtle, Dragon\n Turtle \n") == ["Turtle", "Dragon"]


def test_the_first_object_in_a_reply_is_the_answer():
    twice = 'Sure.\n{"drop": true, "like": 0, "extra": {}}\n\nCorrection:\n{"drop": false}'
    assert judge.first_json(twice) == {"drop": True, "like": 0, "extra": {}}
    assert judge.first_json("no object") is None and judge.first_json(None) is None
    assert judge.first_json('{"drop": tru') is None


def test_a_verdict_is_only_what_the_model_was_asked_for():
    assert judge.verdict({"drop": False, "like": 3, "reason": "x" * 300}) == (
        False,
        3,
        "x" * 120,
    )
    assert judge.verdict({"drop": True, "like": 0}) == (True, 0, "")
    for bad in (
        None,
        {},
        {"drop": "no", "like": 2},
        {"drop": False, "like": 9},
        {"like": True},
    ):
        assert judge.verdict(bad) is None


ASK = judge.listing  # the real one; the fixture swaps it out for the sweeps


class FakeModel:
    model, on = "m", True

    def __init__(self, answers):
        self.answers, self.calls = list(answers), 0

    def ask(self, *_):
        self.calls += 1
        return self.answers.pop(0)


def test_an_answer_that_is_not_a_verdict_is_asked_again(env):
    _, db, _ = env
    model = FakeModel([{"drop": "false", "like": 2}, {"drop": False, "like": 2, "reason": "ok"}])
    assert ASK(db, model, PROFILE, "Turtle", "Shop", [], None) is None
    assert db.one("SELECT COUNT(*) n FROM llm_cache")["n"] == 0
    assert ASK(db, model, PROFILE, "Turtle", "Shop", [], None) == (
        False,
        2,
        "ok",
    )
    assert ASK(db, model, PROFILE, "Turtle", "Shop", [], None) == (
        False,
        2,
        "ok",
    )
    assert model.calls == 2  # the good answer came from the cache the third time


def test_a_photo_the_model_did_not_answer_for_is_not_judged_on_the_title(env):
    _, db, _ = env
    model = FakeModel([None])
    assert ASK(db, model, PROFILE, "Turtle", "Shop", [], b"photo") is None
    assert model.calls == 1


def test_facts_that_are_not_what_was_asked_for_are_asked_again(env):
    _, db, _ = env
    good = dict.fromkeys(judge.FACT_KEYS, "") | {"scale": "1/6"}
    model = FakeModel([{"scale": ["1/6"]}, good])
    assert judge.facts(db, model, "Scale 1/6") is None
    assert judge.facts(db, model, "Scale 1/6") == {"scale": "1/6"}
    assert judge.facts(db, model, "Scale 1/6") == {"scale": "1/6"} and model.calls == 2


def picture(colour):
    from PIL import Image  # pylint: disable=import-outside-toplevel

    buf = io.BytesIO()
    Image.new("RGB", (2000, 1000), colour).save(buf, "PNG")
    return buf.getvalue()


@pytest.fixture
def page(env, monkeypatch):
    """One mailed listing whose own page has two photos; ``reads`` counts the page reads."""
    s, db, state = env
    reads = []

    def detail(*_):
        reads.append(1)
        return {"images": ["https://shop.example/a.png", "https://shop.example/b.png"], "text": ""}

    def fetch(url, *_):
        if url.endswith("b.png") and state.get("b down"):
            raise TimeoutError
        return picture("white" if url.endswith("a.png") else "grey")

    monkeypatch.setattr(sweep.sources, "detail", detail)
    monkeypatch.setattr(sweep.images, "fetch", fetch)
    sweep.run(db, s)
    state["shop"] = ["1", "2", "3"]
    sweep.run(db, s)

    def listing():
        return dict(db.one("SELECT * FROM listings WHERE key = '3'"))

    return s, db, state, reads, listing


def test_a_listing_page_is_read_again_until_it_is_whole(page):
    from PIL import Image  # pylint: disable=import-outside-toplevel

    s, db, state, reads, listing = page
    state["b down"] = True
    model = FakeModel([{"drop": False}, {"drop": False}])
    first = sweep.fill_detail(db, s, model, listing())
    assert first["complete"] is False and len(first["photos"]) == 1
    state["b down"] = False
    again = sweep.fill_detail(db, s, model, listing())
    assert again["complete"] is True and len(again["photos"]) == 2 and len(reads) == 2
    sweep.fill_detail(db, s, model, listing())
    assert len(reads) == 2 and model.calls == 2  # whole: not read or asked about again
    with Image.open(s.img_dir / again["photos"][0]) as big:
        assert max(big.size) == 1600  # the page's copy, not the mail's small one


def test_a_page_photo_is_shown_only_once_the_model_has_cleared_it(page):
    s, db, _, _, listing = page
    held = sweep.fill_detail(db, s, FakeModel([None]), listing())
    assert held["photos"] == [] and held["complete"] is False
    client = TestClient(create_app(s, db))
    assert client.get(f"/listing/{listing()['id']}").status_code == 200  # no model: none shown
    assert listing()["dropped"] == 0
    mail = {"subject": "x", "html": "Item 3", "ids": [listing()["id"]], "at": "", "sent": True}
    db.put(sweep.LAST_MAIL, mail)
    assert client.get("/mail").status_code == 200
    assert sweep.fill_detail(db, s, FakeModel([{"drop": True}]), listing()) == {"dropped": True}
    assert listing()["dropped"] == 1
    assert client.get("/mail").status_code == 404  # the saved mail may have shown it
    assert client.get("/").status_code == 200
    db.run("DELETE FROM seen WHERE key = '3'")  # as if its mail had failed and it were owed
    sweep.run(db, s)
    assert listing()["dropped"] == 1  # the page's verdict stands
    assert client.get(f"/listing/{listing()['id']}").status_code == 404
    assert "Item 3" not in client.get("/new").text


def test_a_listing_whose_photo_could_not_be_fetched_is_held(env):
    s, db, state = env
    sweep.run(db, s)
    wanted = collect.Item(
        "3", "Turtle", "https://shop.example/3", image="https://shop.example/3.jpg"
    )
    state["shop"] = ["1", "2", wanted]
    sweep.run(db, s)
    assert state["sent"] == [] and state["asked"] == []  # not judged on its title


def test_a_verdict_on_words_alone_ends_when_a_photo_comes():
    bare = collect.Item("3", "Turtle", "https://shop.example/3")
    shown = collect.Item(
        "3", "Turtle", "https://shop.example/3", image="https://shop.example/3.jpg"
    )
    assert sweep.sig(bare) != sweep.sig(shown)


def test_a_photo_that_will_not_download_uses_none_of_the_cap(env, monkeypatch):
    s, db, state = env
    monkeypatch.setenv("TOYS_JUDGE_CAP", "1")
    s = config.load()
    sweep.run(db, s)
    stuck = collect.Item(
        "3", "Turtle", "https://shop.example/3", image="https://shop.example/3.jpg"
    )
    state["shop"] = ["1", "2", stuck, "4"]
    sweep.run(db, s)
    assert state["asked"] == ["Item 4"]  # the one behind it is still judged


def test_a_price_that_is_not_a_number_is_no_price():
    num = collection._num  # pylint: disable=protected-access
    assert num("Infinity") is None and num(1e999) is None and num("12.5") == 12.5


def test_a_listing_owed_a_mail_is_kept_after_the_shop_stops_showing_it(env):
    s, db, state = env
    sweep.run(db, s)
    state["shop"], state["mail"] = ["1", "2", "3"], "failed: relay down"
    sweep.run(db, s)
    state["shop"], state["mail"] = ["1", "2"], "sent"  # pushed off the page read
    sweep.run(db, s)
    sweep.run(db, s)
    assert subjects(state) == ["Toys: 1 for you, 0 other new"]


def test_a_shop_description_is_kept_as_text_only():
    got = sources.woo_detail(
        {
            "images": [
                {"src": "https://shop.example/a.jpg"},
                {"src": "javascript:alert(1)"},
            ],
            "description": "<p>Scale: 1/6</p><script>alert(1)</script><ul><li>Height 40cm</li></ul>",
        }
    )
    assert got == {
        "images": ["https://shop.example/a.jpg"],
        "text": "Scale: 1/6\nHeight 40cm",
    }
    shop = sources.Shop("S", "page", "https://s.example", img_match=r"/products/")
    page = {
        "images": ["https://s.example/products/1.jpg", "https://s.example/logo.png"],
        "og": 5,
    }
    assert sources.page_detail(page, shop)["images"] == ["https://s.example/products/1.jpg"]
    coded = sources.Shop("S", "page", "https://s.example", detail_match=r"/images/{slug}/")
    page = {
        "images": [
            "https://s.example/images/AB-1/a.jpg",
            "https://s.example/images/CD-2/b.jpg",
        ]
    }
    got = sources.page_detail(page, coded, "https://s.example/x/AB-1.html?q=1")
    assert got["images"] == ["https://s.example/images/AB-1/a.jpg"]
    # a product page that also shows other products: only this product's photos are kept
    own = sources.Shop("S", "page", "https://s.example", img_match="/p/", own=r"/p/(\d+)/")
    page = {"images": ["https://s.example/p/7/a.jpg", "https://s.example/p/8/b.jpg"]}
    page["og"] = "https://s.example/p/9/other.jpg"
    got = sources.page_detail(page, own, image="https://s.example/p/7/small.jpg")
    assert got["images"] == ["https://s.example/p/7/a.jpg"]
    assert not sources.page_detail(page, own)["images"]


RECORDS = [
    {"id": "T1", "item": "Turtle statue", "maker": "Good Studio", "vendor": "Shop A"}
    | {
        "date": "2024-01-02",
        "gbp_paid": 100.5,
        "franchise": "Monsters",
        "state": "received",
    },
    {"id": "T2", "item": "Dragon statue", "vendor": "Shop B", "gbp_paid": None}
    | {
        "state": "chase: balance/ETA",
        "notes": ["asked in May"],
        "franchise": "Monsters",
    },
    {
        "id": "T3",
        "item": "Robot",
        "vendor": "Shop A",
        "gbp_paid": 20,
        "state": "refunded",
    },
    {"id": "T4", "item": "Folded away", "state": "merged"},
]


def test_the_collection_loads_as_a_whole_and_pages_show_it(env):
    s, db, _ = env
    assert collection.replace(db, RECORDS) == 3
    assert collection.owned_counts(db) == {"Monsters": 2}
    with pytest.raises(ValueError):
        collection.replace(db, [{"id": "X"}])
    assert len(collection.find(db)) == 3  # a refused load changes nothing
    client = TestClient(create_app(s, db))
    home = client.get("/").text
    assert "Turtle statue" in home and "£120.50 paid" in home and "1 with no price" in home
    assert "Dragon statue" in client.get("/orders").text
    assert "Turtle statue" not in client.get("/orders").text
    assert "Robot" not in client.get("/?show=have").text
    assert "asked in May" in client.get("/item/T2").text
    assert client.get("/item/T4").status_code == 404
    up = client.post(
        "/import",
        data={"kind": "collection"},
        files={"file": ("c.json", json.dumps(RECORDS[:1]), "application/json")},
        follow_redirects=False,
    )
    assert up.status_code == 303 and len(collection.find(db)) == 1
    bad = client.post("/import", data={"kind": "collection"}, files={"file": ("c.json", "{")})
    assert bad.status_code == 400 and len(collection.find(db)) == 1


def test_likes_are_edited_in_the_app(env):
    s, db, _ = env
    client = TestClient(create_app(s, db))
    assert "Turtle, Dragon" in client.get("/profile").text
    form = {
        "chars:Monsters": "Turtle\nBird",
        "never": "Villain, Rival",
        "notes": "One.\nTwo.",
    }
    form |= {"add_franchise": "Robots", "add_characters": "Unit One"}
    assert client.post("/profile", data=form, follow_redirects=False).status_code == 303
    p = profile.load(db)
    assert p["characters"] == {"Monsters": ["Turtle", "Bird"], "Robots": ["Unit One"]}
    assert p["never"] == ["Villain", "Rival"] and p["notes"] == ["One.", "Two."]
    assert p["studios"] == {"Good Studio": 2}  # parts the form does not carry are kept


def test_pages_health_and_metrics(env):
    s, db, state = env
    sweep.run(db, s)
    state["shop"] = ["1", "2", "3"]
    sweep.run(db, s)
    client = TestClient(create_app(s, db))
    assert "Item 3" in client.get("/new").text and "Item 1" not in client.get("/new").text
    shown = db.one("SELECT id FROM listings WHERE key = '3'")["id"]
    assert "Open at Shop" in client.get(f"/listing/{shown}").text
    assert (
        "Item 3" in client.get("/mail").text and "Shop: ok, 3 listings" in client.get("/log").text
    )
    assert client.get("/health").json()["ok"] is True
    body = client.get("/metrics").text
    assert "toys_listings 3" in body and "toys_last_success_timestamp_seconds 0" not in body
    assert client.get("/img/..%2Ftoys.db").status_code == 404
