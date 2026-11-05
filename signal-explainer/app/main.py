"""FastAPI app: web UI (Jinja) and a JSON API used by the Claude skill and the ChatGPT GPT action.

Everyone signs in with their own Bigdata API key and sees only their own runs and batches.
"""
from __future__ import annotations

import asyncio
import csv
import io
import logging
import re
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import Literal
from urllib.parse import quote, urlparse

import markdown as md
from fastapi import Depends, FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape
from pydantic import BaseModel, Field

from . import analysis, auth
from .auth import LoginRequired, User
from .bigdata import BigdataClient, BigdataError
from .charts import sentiment_svg
from .config import get_settings
from .pipeline import run_explain
from .report import to_markdown, windows
from .store import Store, now_iso

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
HERE = Path(__file__).parent
settings = get_settings()
store = Store(settings.DB_PATH)
_tasks: set[asyncio.Task] = set()


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.bd = BigdataClient(settings, store)
    if settings.BIGDATA_API_KEY:
        # Runs made before sign-in existed go to whoever owns the key in .env.
        store.claim_unowned(auth.fingerprint(settings.BIGDATA_API_KEY))
    # Runs interrupted by a restart would otherwise stay "running" forever.
    store.execute("UPDATE runs SET status='error', error='Interrupted by a server restart' "
                  "WHERE status IN ('queued','running')")
    yield
    await app.state.bd.close()


# No interactive docs pages; the JSON API under /api stays for the Claude skill and the ChatGPT GPT.
app = FastAPI(title="Signal Explainer", version="0.1.0", lifespan=lifespan, docs_url=None, redoc_url=None,
              description="Explain why a company's news-driven sentiment moved, as a narrative for fundamental PMs. "
                          "Data from Bigdata.com.",
              servers=[{"url": "/"}])
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=HERE / "templates")
templates.env.filters["pct"] = lambda v: "—" if v is None else f"{v:.0%}"
templates.env.filters["signed"] = lambda v, n=2: "—" if v is None else f"{v:+.{n}f}"
templates.env.globals["describe_move"] = analysis.describe_move

LOOKBACKS = [7, 14, 30, 60, 90]


def _spawn(coro) -> None:
    t = asyncio.create_task(coro)
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)


def _bd(user: User) -> BigdataClient:
    return app.state.bd.for_key(user.api_key, user.id)


def _start_run(user: User, ticker: str, as_of: date, lookback: int, mode: str, score: float | None = None,
               batch_id: int | None = None) -> int:
    rid = store.create_run(ticker=ticker.strip().upper(), as_of=as_of.isoformat(), lookback_days=lookback, mode=mode,
                           input_score=score, batch_id=batch_id, user_id=user.id)
    if batch_id is None:
        _spawn(run_explain(rid, settings, store, _bd(user)))
    return rid


def _own_run(run_id: int, user: User | None) -> dict:
    """The run if it is a public demo or belongs to ``user``; otherwise 404, so other users' run ids reveal nothing."""
    run = store.get_run(run_id)
    if not run or not (run.get("public") or (user and run.get("user_id") == user.id)):
        raise HTTPException(404, "Run not found")
    return run


def _own_batch(batch_id: int, user: User) -> dict:
    b = store.one("SELECT * FROM batches WHERE id=? AND user_id=?", (batch_id, user.id))
    if not b:
        raise HTTPException(404, "Batch not found")
    return b


# --- sign-in -----------------------------------------------------------------------------------------------------
def optional_user(request: Request) -> User | None:
    """Public pages: the signed-in user if there is one (also shown in the header)."""
    key = auth.cookie_key(request)
    if not key:
        return None
    uid = auth.fingerprint(key)
    request.state.user = user = User(uid, key, (store.get_user(uid) or {}).get("name"))
    return user


def web_user(request: Request) -> User:
    """Web routes that run or list a user's own work: the signed-in user, or a redirect to /login."""
    if not (user := optional_user(request)):
        raise LoginRequired()
    return user


async def api_user(request: Request) -> User:
    """API routes: `Authorization: Bearer <Bigdata API key>` (or X-API-KEY), checked against Bigdata.com."""
    key = auth.header_key(request) or auth.cookie_key(request)
    if not key:
        raise HTTPException(401, "Send your Bigdata API key as 'Authorization: Bearer <key>'")
    uid = auth.fingerprint(key)
    if not auth.is_verified(key):
        try:
            q = await app.state.bd.for_key(key, uid).quotas()
        except BigdataError as e:
            raise HTTPException(401 if e.status in (401, 403) else 502, "Bigdata.com rejected this API key"
                                if e.status in (401, 403) else str(e)) from e
        store.touch_user(uid, org_id=(q or {}).get("org_id"))
        auth.mark_verified(key)
    return User(uid, key, (store.get_user(uid) or {}).get("name"))


@app.exception_handler(LoginRequired)
async def login_required(request: Request, _: LoginRequired):
    # A form post can't be replayed after sign-in, so send the user back to the page it came from.
    nxt = request.url.path + (f"?{request.url.query}" if request.url.query else "") if request.method == "GET" else (
        urlparse(request.headers.get("referer") or "/").path or "/")
    resp = RedirectResponse(f"/login?next={quote(nxt)}", status_code=303)
    auth.clear_key_cookie(resp)
    return resp


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, next: str = "/"):
    return templates.TemplateResponse(request, "login.html", {"next": _safe_next(next), "error": None})


@app.post("/login")
async def login(request: Request, api_key: str = Form(...), name: str = Form(""), next: str = Form("/")):
    key, nxt = api_key.strip(), _safe_next(next)
    err = None
    if not key or len(key) > 512:
        err = "Enter your Bigdata API key."
    else:
        uid = auth.fingerprint(key)
        try:
            q = await app.state.bd.for_key(key, uid).quotas()
        except BigdataError as e:
            err = ("Bigdata.com did not accept this key." if e.status in (401, 403)
                   else f"Could not reach Bigdata.com to check the key ({e.status}). Try again.")
    if err:
        return templates.TemplateResponse(request, "login.html", {"next": nxt, "error": err, "name": name},
                                          status_code=401)
    store.touch_user(uid, name=name.strip()[:60] or None, org_id=(q or {}).get("org_id"))
    auth.mark_verified(key)
    resp = RedirectResponse(nxt, status_code=303)
    auth.set_key_cookie(resp, key)
    return resp


@app.post("/logout")
async def logout():
    resp = RedirectResponse("/login", status_code=303)
    auth.clear_key_cookie(resp)
    return resp


def _safe_next(nxt: str) -> str:
    """Only same-site paths, so the login form can't be used to bounce users to another site."""
    return nxt if nxt.startswith("/") and not nxt.startswith("//") and "\\" not in nxt else "/"


# --- web UI ------------------------------------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def home(request: Request, user: User | None = Depends(optional_user)):
    return templates.TemplateResponse(request, "home.html", {
        "examples": store.public_runs(), "recent": store.recent_runs(user.id) if user else [], "today": date.today().isoformat(), "lookbacks": LOOKBACKS,
        "model": settings.OPENAI_MODEL})


@app.post("/explain")
async def explain_form(ticker: str = Form(...), as_of: date = Form(...), lookback: str = Form("30"),
                       user: User = Depends(web_user)):
    mode = "since_earnings" if lookback == "earnings" else "lookback"
    rid = _start_run(user, ticker, as_of, 0 if mode == "since_earnings" else int(lookback), mode)
    return RedirectResponse(f"/runs/{rid}", status_code=303)


@app.get("/runs/{run_id}", response_class=HTMLResponse)
async def run_page(request: Request, run_id: int, user: User | None = Depends(optional_user)):
    run = _own_run(run_id, user)
    ctx = {"run": run, "chart": ""}
    if run["status"] == "done":
        f = run["facts"]
        ctx["chart"] = sentiment_svg(f["chart"]["series"], date.fromisoformat(f["window"]["start"]),
                                     date.fromisoformat(f["as_of"]), f["move"]["inflection_days"], f["chart"]["peers"])
        ctx["evidence"] = {e["id"]: e for e in f["attribution"]["evidence"]}
        ctx["titles"] = {d["key"]: d for d in run["narrative"].get("drivers", [])}
        ctx["markdown"] = to_markdown(run, str(request.base_url).rstrip("/"))
        ctx["ws"] = windows(f)
    return templates.TemplateResponse(request, "run.html", ctx)


@app.get("/runs/{run_id}/markdown", response_class=PlainTextResponse)
async def run_markdown(request: Request, run_id: int, user: User | None = Depends(optional_user)):
    run = _own_run(run_id, user)
    if run["status"] != "done":
        raise HTTPException(404, "Run not finished")
    return to_markdown(run, str(request.base_url).rstrip("/"))


@app.get("/batch", response_class=HTMLResponse)
async def batch_form(request: Request, user: User = Depends(web_user)):
    batches = store.all("SELECT b.*, COUNT(r.id) n FROM batches b LEFT JOIN runs r ON r.batch_id=b.id "
                        "WHERE b.user_id=? GROUP BY b.id ORDER BY b.id DESC LIMIT 20", (user.id,))
    return templates.TemplateResponse(request, "batch.html", {
        "batches": batches, "today": date.today().isoformat(), "lookbacks": LOOKBACKS})


@app.post("/batch")
async def batch_create(rows: str = Form(""), file: UploadFile | None = None, as_of: date = Form(...),
                       lookback: str = Form("30"), name: str = Form(""), user: User = Depends(web_user)):
    text = rows
    if file is not None and file.filename:
        text = (await file.read()).decode("utf-8", "replace")
    items = parse_batch(text, as_of)
    if not items:
        raise HTTPException(400, "No tickers found. Use one per line: TICKER[,YYYY-MM-DD][,score]")
    bid = create_batch(user, items, lookback, name)
    return RedirectResponse(f"/batches/{bid}", status_code=303)


@app.get("/batches/{batch_id}", response_class=HTMLResponse)
async def batch_page(request: Request, batch_id: int, user: User = Depends(web_user)):
    b = _own_batch(batch_id, user)
    runs = store.batch_runs(batch_id)
    ranked = sorted(runs, key=lambda r: (r["status"] != "done", -(r["surprise"] or 0)))
    return templates.TemplateResponse(request, "batch_view.html", {"batch": b, "runs": ranked})


@app.get("/method", response_class=HTMLResponse)
async def method(request: Request, _: User | None = Depends(optional_user)):
    return templates.TemplateResponse(request, "method.html", {"model": settings.OPENAI_MODEL})


def parse_batch(text: str, default_as_of: date) -> list[tuple[str, date, float | None]]:
    out = []
    for row in csv.reader(io.StringIO(text)):
        cells = [c.strip() for c in row if c.strip()]
        if not cells or cells[0].lower() in ("ticker", "symbol"):
            continue
        d, score = default_as_of, None
        for c in cells[1:]:
            try:
                d = date.fromisoformat(c)
                continue
            except ValueError:
                pass
            try:
                score = float(c)
            except ValueError:
                pass
        out.append((cells[0], d, score))
    return out


def create_batch(user: User, items: list[tuple[str, date, float | None]], lookback: str, name: str = "") -> int:
    mode = "since_earnings" if lookback == "earnings" else "lookback"
    bid = store.execute("INSERT INTO batches (created_at, name, status, user_id) VALUES (?,?,?,?)",
                        (now_iso(), name or f"{len(items)} names", "running", user.id))
    ids = [_start_run(user, t, d, 0 if mode == "since_earnings" else int(lookback), mode, s, bid)
           for t, d, s in items]
    bd = _bd(user)

    async def runner():
        sem = asyncio.Semaphore(settings.BATCH_CONCURRENCY)

        async def one(rid: int):
            async with sem:
                await run_explain(rid, settings, store, bd)

        await asyncio.gather(*(one(r) for r in ids))
        store.execute("UPDATE batches SET status='done' WHERE id=?", (bid,))

    _spawn(runner())
    return bid


# --- JSON API ----------------------------------------------------------------------------------------------------
class ExplainRequest(BaseModel):
    ticker: str = Field(..., description="Ticker, company name or Bigdata.com rp_entity_id, e.g. NVDA")
    as_of: date | None = Field(None, description="Analysis date (YYYY-MM-DD). Defaults to today. Point-in-time.")
    lookback_days: int = Field(30, ge=3, le=180, description="Window length in days (ignored for since_earnings)")
    mode: Literal["lookback", "since_earnings"] = Field("lookback", description="'since_earnings' starts the window "
                                                        "at the last earnings call")
    input_score: float | None = Field(None, description="Optional: the client's own score for this name and date")
    wait_seconds: int = Field(40, ge=0, le=110, description="How long to wait for the result before returning the "
                                                            "run id to poll")


class BatchRequest(BaseModel):
    tickers: list[str] = Field(..., description="Tickers to explain", max_length=50)
    as_of: date | None = None
    lookback_days: int = 30
    mode: Literal["lookback", "since_earnings"] = "lookback"


def run_json(request: Request, run: dict) -> dict:
    base = str(request.base_url).rstrip("/")
    out = {"run_id": run["id"], "status": run["status"], "progress": run["progress"], "error": run["error"],
           "ticker": run["ticker"], "company": run["company"], "as_of": run["as_of"],
           "report_url": f"{base}/runs/{run['id']}"}
    if run["status"] == "done":
        f = run["facts"]
        out |= {"narrative": run["narrative"], "move": {k: v for k, v in f["move"].items()},
                "window": f["window"], "peers": f["peers"],
                "drivers": f["attribution"]["drivers"], "evidence": f["attribution"]["evidence"],
                "markdown": to_markdown(run, base)}
    return out


@app.post("/api/explain", operation_id="explainSentiment",
          summary="Explain why a company's news sentiment moved over a window")
async def api_explain(req: ExplainRequest, request: Request, user: User = Depends(api_user)) -> dict:
    as_of = req.as_of or date.today()
    rid = _start_run(user, req.ticker, as_of, req.lookback_days, req.mode, req.input_score)
    for _ in range(req.wait_seconds * 2):
        run = store.get_run(rid)
        if run["status"] in ("done", "error"):
            break
        await asyncio.sleep(0.5)
    return run_json(request, store.get_run(rid))


@app.get("/api/runs/{run_id}", operation_id="getRun",
         summary="Get a run's status and, when done, its narrative, drivers and evidence")
async def api_run(run_id: int, request: Request, user: User = Depends(api_user)) -> dict:
    return run_json(request, _own_run(run_id, user))


@app.post("/api/batch", operation_id="explainBatch",
          summary="Explain several names at once; returns a batch id to poll")
async def api_batch(req: BatchRequest, request: Request, user: User = Depends(api_user)) -> dict:
    as_of = req.as_of or date.today()
    lb = "earnings" if req.mode == "since_earnings" else str(req.lookback_days)
    bid = create_batch(user, [(t, as_of, None) for t in req.tickers], lb)
    return {"batch_id": bid, "batch_url": f"{str(request.base_url).rstrip('/')}/batches/{bid}"}


@app.get("/api/batches/{batch_id}", operation_id="getBatch",
         summary="Batch status with each name's headline, ranked by how unusual the move is")
async def api_batch_get(batch_id: int, user: User = Depends(api_user)) -> dict:
    b = _own_batch(batch_id, user)
    runs = sorted(store.batch_runs(batch_id), key=lambda r: -(r["surprise"] or 0))
    return {"batch": b, "runs": runs}


def _cite(text: str | None) -> Markup:
    """Escape, then turn [E3] / [M1] citations into links to the evidence list."""
    return Markup(re.sub(r"\[((?:E|M)\d+)\]", r'<a class="cite" href="#\1">\1</a>', str(escape(text or ""))))


templates.env.filters["md"] = lambda s: Markup(md.markdown(s or ""))
templates.env.filters["cite"] = _cite
