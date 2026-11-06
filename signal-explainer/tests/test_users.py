"""Each user signs in with their own Bigdata API key and sees only their own runs and batches."""
import os
import tempfile

os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "test.db")
os.environ["BIGDATA_API_KEY"] = ""

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import main  # noqa: E402
from app.bigdata import BigdataClient, BigdataError  # noqa: E402

ALICE, BOB = "good-key-alice", "good-key-bob"


@pytest.fixture(autouse=True)
def fake_bigdata(monkeypatch):
    async def quotas(self):
        if not self.api_key.startswith("good"):
            raise BigdataError(401, "invalid key")
        return {"org_id": "org-" + self.api_key[-3:]}

    async def no_run(*a, **k):
        return None

    monkeypatch.setattr(BigdataClient, "quotas", quotas)
    monkeypatch.setattr(main, "run_explain", no_run)


def browser(key=None, name=""):
    c = TestClient(main.app, base_url="https://testserver")
    c.__enter__()
    if key:
        r = c.post("/login", data={"api_key": key, "name": name, "next": "/"}, follow_redirects=False)
        assert r.status_code == 303
    return c


def explain(c, ticker):
    r = c.post("/explain", data={"ticker": ticker, "as_of": "2026-10-28", "lookback": "30"}, follow_redirects=False)
    return int(r.headers["location"].rsplit("/", 1)[1])


def test_dashboard_is_public_but_running_needs_sign_in():
    c = browser()
    assert c.get("/").status_code == 200 and "Sign in" in c.get("/").text
    assert c.get("/method").status_code == 200
    r = c.post("/explain", data={"ticker": "NVDA", "as_of": "2026-10-28", "lookback": "30"},
               headers={"referer": "https://testserver/"}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login?next=%2F&ticker=NVDA&as_of=2026-10-28&lookback=30"
    r = c.get("/batch", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login?next=%2Fbatch")


def test_explain_form_survives_sign_in():
    c = browser()
    r = c.post("/explain", data={"ticker": "lly", "as_of": "2026-10-28", "lookback": "earnings"},
               headers={"referer": "https://testserver/"})
    assert "explains <strong>LLY</strong> as of 2026-10-28, since the last earnings call" in r.text
    # A wrong key keeps the pending run on the page.
    r = c.post("/login", data={"api_key": "wrong", "next": "/", "ticker": "LLY", "as_of": "2026-10-28",
                               "lookback": "earnings"})
    assert r.status_code == 401 and 'name="ticker" value="LLY"' in r.text
    # Signing in starts the run and lands on it.
    r = c.post("/login", data={"api_key": ALICE, "next": "/", "ticker": "LLY", "as_of": "2026-10-28",
                               "lookback": "earnings"}, follow_redirects=False)
    rid = int(r.headers["location"].rsplit("/", 1)[1])
    run = main.store.get_run(rid)
    assert (run["ticker"], run["as_of"], run["mode"]) == ("LLY", "2026-10-28", "since_earnings")
    assert c.get(f"/runs/{rid}").status_code == 200


def test_sign_in_ignores_a_malformed_pending_run():
    c = browser()
    assert "pending-run" not in c.get("/login?next=/&ticker=NVDA&as_of=not-a-date&lookback=30").text
    r = c.post("/login", data={"api_key": ALICE, "next": "/", "ticker": "NVDA", "as_of": "2026-10-28",
                               "lookback": "abc"}, follow_redirects=False)
    assert r.headers["location"] == "/"


def test_public_demo_runs_open_to_everyone():
    alice = browser(ALICE)
    demo, private = explain(alice, "NVDA"), explain(alice, "META")
    main.store.execute("UPDATE runs SET public=1 WHERE id=?", (demo,))
    anon, bob = browser(), browser(BOB)
    for c in (anon, bob):
        assert c.get(f"/runs/{demo}").status_code == 200
        assert c.get(f"/runs/{private}").status_code == 404
        assert f"/runs/{demo}" in c.get("/").text
    assert f"/runs/{private}" not in anon.get("/").text


def test_bad_key_is_rejected():
    c = browser()
    r = c.post("/login", data={"api_key": "wrong", "next": "/"}, follow_redirects=False)
    assert r.status_code == 401 and "did not accept" in r.text
    assert "se_key" not in c.cookies


def test_key_is_not_stored_in_cookie_or_db():
    c = browser(ALICE, "Alice")
    assert ALICE not in c.cookies["se_key"]
    rows = main.store.all("SELECT * FROM users")
    assert all(ALICE not in str(r) for r in rows)
    assert "Alice" in c.get("/").text


def test_users_see_only_their_own_runs():
    alice, bob = browser(ALICE, "Alice"), browser(BOB, "Bob")
    rid = explain(alice, "NVDA")
    assert alice.get(f"/runs/{rid}").status_code == 200
    assert bob.get(f"/runs/{rid}").status_code == 404
    assert bob.get(f"/runs/{rid}/markdown").status_code == 404
    assert f"/runs/{rid}" in alice.get("/").text
    assert f"/runs/{rid}" not in bob.get("/").text


def test_batches_are_private():
    alice, bob = browser(ALICE), browser(BOB)
    r = alice.post("/batch", data={"rows": "AAPL\nMSFT", "as_of": "2026-10-28", "lookback": "30"},
                   follow_redirects=False)
    bid = int(r.headers["location"].rsplit("/", 1)[1])
    assert alice.get(f"/batches/{bid}").status_code == 200
    assert bob.get(f"/batches/{bid}").status_code == 404


def test_api_uses_bearer_key_and_ownership():
    c = TestClient(main.app, base_url="https://testserver")
    assert c.post("/api/explain", json={"ticker": "NVDA"}).status_code == 401
    assert c.post("/api/explain", json={"ticker": "NVDA"},
                  headers={"Authorization": "Bearer wrong"}).status_code == 401
    r = c.post("/api/explain", json={"ticker": "NVDA", "wait_seconds": 0},
               headers={"Authorization": f"Bearer {ALICE}"})
    rid = r.json()["run_id"]
    assert c.get(f"/api/runs/{rid}", headers={"Authorization": f"Bearer {ALICE}"}).status_code == 200
    assert c.get(f"/api/runs/{rid}", headers={"X-API-KEY": BOB}).status_code == 404


def test_login_redirect_stays_on_site():
    c = browser()
    r = c.post("/login", data={"api_key": ALICE, "next": "//evil.example"}, follow_redirects=False)
    assert r.headers["location"] == "/"
