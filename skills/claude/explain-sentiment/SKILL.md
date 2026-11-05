---
name: explain-sentiment
description: Explain WHY a company's news-driven sentiment score moved over a lookback window, as a cited narrative a fundamental PM can read in a minute. Breaks the move into the events that drove it (EDGE event types), says whether it was stock-specific or sector-wide, and, in "since last earnings" mode, compares what management said on the call with the news since. Use whenever someone asks why sentiment / a sentiment score / an EI score moved for a stock, wants a "what changed" or between-earnings check on a name, wants a narrative behind a quant signal for the fundamental team, or gives a list of tickers or score outliers to explain. Triggers - "explain NVDA sentiment over 30 days", "why did META's score drop", "what changed for LLY since earnings", "narrative for these movers", "/explain-sentiment AVGO 2026-10-15 30d". Powered by Bigdata.com.
---

# Explain a sentiment move

Turn a quant sentiment signal into a narrative for fundamental PMs: **how unusual the move is, what drove it, whether it was the stock or the sector, and what to watch.** Data comes only from [Bigdata.com](https://bigdata.com).

## Inputs

Parse from the request; ask only if the company is missing.

| Input | Default | Notes |
|---|---|---|
| company | — | ticker, name or rp_entity_id; a list means batch |
| as_of | today | point in time: never use anything published after it |
| window | 30 days | `7d`/`30d`/`90d`, or `since earnings` (window starts at the last earnings call) |
| input_score | none | the user's own score for the name, if given; quote it, don't reinterpret it |

## Route A: the Signal Explainer app (preferred when available)

If `SIGNAL_EXPLAINER_URL` is set (or `http://localhost:8077` answers) and `BIGDATA_API_KEY` is set, call the app. It runs the full deterministic attribution and returns a ready report. The app signs callers in with their own Bigdata API key: the run is billed to that account and only that key can read it back. If `BIGDATA_API_KEY` is not set, ask the user to export it, or use Route B. Never print the key.

```bash
curl -s -X POST "$SIGNAL_EXPLAINER_URL/api/explain" \
  -H 'content-type: application/json' -H "Authorization: Bearer $BIGDATA_API_KEY" \
  -d '{"ticker":"NVDA","as_of":"2026-10-27","lookback_days":30,"mode":"lookback","wait_seconds":100}'
```

- `mode` is `"lookback"` or `"since_earnings"`. If `status` is not `done` yet, poll `GET /api/runs/{run_id}` every 10 s.
- Show the user the returned `markdown` field **as-is**, then the `report_url` link (the web page has the chart and the evidence; it opens after signing in with the same key).
- A `401` means the key is missing or Bigdata.com rejected it. A `404` on a run id means it belongs to another key.
- For a list, `POST /api/batch {"tickers":[...], "as_of":..., "lookback_days":...}`, poll `GET /api/batches/{id}` until done, then show a table ranked by `surprise` (headline, Δ sentiment, σ) and offer to open any single report.

## Route B: Bigdata.com MCP only (no app)

Use the Bigdata.com MCP tools. Work through these steps; run independent calls in parallel.

1. **Resolve.** `find_securities` with the ticker, which gives you `id` (rp_entity_id), industry and sector.
2. **Window.** For a fixed window, set `start = as_of − N days` and use a prior window of equal length right before it. For `since earnings`, run `bigdata_search` fast mode with `entity.any_of=[id]`, `document_type INCLUDE [{"type":"TRANSCRIPT","subtypes":["EARNINGS_CALL"]}]`, timestamp `as_of−200d … as_of`, and take the latest call date as `start`.
3. **Level now.** If `as_of` is today, call `bigdata_sentiment_tearsheet(id)` for the current score, direction and attention. It is real-time only, so skip it for past dates.
4. **News in both windows.** Run fast searches with no `text`, using these filters: `entity: {"search_in": "HEADLINE", "any_of": [id]}`, `category INCLUDE ["news"]`, `max_chunks: 15`. Run one search for each **half of each window** × `sentiment.values ["positive"]` / `["negative"]`, which is 8 searches run in parallel.
   - Headline matching keeps the articles that are *about* the company. Matching anywhere in the body, or adding a `text` query, fills the results with passing mentions such as market wraps and ad boilerplate.
   - Splitting each window matters. Results lean toward the most recent days, so one search over a whole window misses events early in it. In testing, a single 30-day search missed an FT lawsuit story that drove the move.
5. **Attribute.** Read the passages and cluster them into 3–6 **event drivers** (for example: lawsuit, product launch, guidance, partnership, M&A, regulation, analyst actions). Count only what a passage says *about the target company*. For each driver, compare current-window and prior-window volume and tone, then label it **lifted** (new positive or faded negative) or **weighed** (new negative or faded positive). Treat stock-price and index commentary as **market reaction**, not a cause.
6. **Peers.** Pick 3–4 same-industry names that come up in the passages and resolve them with `find_securities`.
   - **If `as_of` is today:** call `bigdata_sentiment_tearsheet` for each peer and the target, then compare `sentiment.momentum` and `zscore_1mo`. This is one call per name.
   - **For a past date:** for each peer, run one headline-entity search per window, positive and negative, and compare the counts.

   Your verdict is **stock-specific** (the target diverges from its peers), **sector-wide** (everyone moved together) or **mixed**.
7. **Management (since-earnings only).** Run a fast search on that call with `sections.any_of ["MANAGEMENT-DISCUSSION"]`, `text: "outlook guidance priorities"`, `max_chunks: 15`. Cite these passages as [M1]…
8. **Write** the report below.

Route B approximates the app's attribution; it isn't the same calculation. It reads about 120 passages per window where the app reads up to about 720, and it has no history of daily sentiment to score how unusual the move is. The tearsheet's `momentum` measures the move against a 90-day baseline, not the change over your window. Say so in the caveats. In testing on NVDA (2026-10-28, 30 days), Route B found the same verdict as the app (stock-specific) and the same main storylines, and added context the app's top-6 drivers left out.

## Output (both routes)

```markdown
# {Company} ({TICKER}): {headline, ≤14 words}
*As of {as_of} · {window description} · tone: {positive|negative|mixed|neutral} · confidence: {high|medium|low}*

**Signal:** sentiment {from} → {to} ({change}), {how unusual}. {Input score if given.}

**Summary.** Two sentences a PM reads first.

## What happened
120–200 words, every claim cited [E1].

## What drove the change
| Driver | Effect | Share | Articles now / prior | Why |

## Stock-specific or sector-wide?
Verdict plus the peer numbers.

## Since the last earnings call   ← since-earnings mode only
What management said [M1] vs what the news says now [E4].

## What to watch
## Questions for management
## Evidence
- **[E1]** date · source · [headline](url) (sentiment ±0.xx)
*Source: Bigdata.com.*
```

## Rules

- **Point-in-time:** nothing published after `as_of`, and say "as of" in the header.
- **Cite everything.** Use only facts in the retrieved passages. Never invent numbers, dates or events, and never link a URL that didn't come from a tool result.
- Explain the **change**, not just the level. If the news you found doesn't explain the direction of the move, say so plainly; that is useful to a quant too.
- Separate allegations from findings (lawsuits) and reaction from cause (price moves).
- No buy/sell recommendations.
- Keep the whole report readable in about a minute; detail goes in Evidence.
