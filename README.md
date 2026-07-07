# SteamBot

[![CI](https://github.com/coreystevensdev/steambot/actions/workflows/ci.yml/badge.svg)](https://github.com/coreystevensdev/steambot/actions)
[![49 tests](https://img.shields.io/badge/tests-49-brightgreen)](https://github.com/coreystevensdev/steambot/actions)
[![18-case eval](https://img.shields.io/badge/eval-18%20cases-blue)](eval/dataset.jsonl)

Agentic NFL betting research service that finds closing line value before the market closes. Pulls Pinnacle sharp-book lines via The Odds API, strips vig to no-vig fair probabilities, then uses Claude to surface picks where retail prices measurably beat the sharp-market consensus. LangGraph HITL checkpoint requires user approval before any bet slip is prepared.

## Problem

Retail sports bettors lose because they bet off public lines that already carry bookmaker margin. Closing Line Value (CLV) is the market-validated signal that separates long-run winners from losers: if you consistently beat the closing line, you have genuine edge. No public tool automates this research pipeline end-to-end with HITL approval built in.

## Solution

The pipeline runs as a LangGraph StateGraph: fetch Pinnacle odds, strip vig to no-vig fair probabilities for each side, filter picks by a minimum edge threshold, then call Claude with a forced `submit_picks` tool to generate structured pick candidates. An `interrupt()` checkpoint pauses the graph for user approval before any bet slip is finalized. State persists via `PostgresSaver` so approval sessions survive server restarts. CLV is recorded post-settlement for every approved pick, building a backtestable track record.

---

## How it works

```mermaid
flowchart TD
    A[POST /api/runs] --> B[OddsAgent: fetch NFL odds]
    B --> C[Compute no-vig fair probs from Pinnacle]
    C --> D[PickAgent: Claude forced tool call]
    D --> E{LangGraph interrupt}
    E --> F[User reviews candidates via GET /api/runs/id]
    F --> G[POST /api/runs/id/approve]
    G --> H[ValidateAgent: record CLV baseline]
    H --> I[Bet slips prepared]
```

**Data source routing:**

| Source | Purpose | Access |
|---|---|---|
| Pinnacle (via The Odds API) | Sharp-line reference; no-vig fair probability | `ODDS_API_KEY` |
| FanDuel / DraftKings / BetMGM | Retail price comparison; line shopping | Same key |
| Anthropic Claude | Pick generation with forced `submit_picks` tool call | `ANTHROPIC_API_KEY` |
| Stripe | Subscription billing (Pro tier) | `STRIPE_SECRET_KEY` |

---

## Tech stack

| Layer | Technology | Why |
|---|---|---|
| Agent orchestration | LangGraph 0.3+ | Stateful graph with first-class `interrupt()` for HITL; `PostgresSaver` for durable checkpoints across restarts |
| LLM | Anthropic Claude (forced tool call) | Forced tool use (`submit_picks`) guarantees structured output; no output parsing |
| Odds data | The Odds API v4 | Single endpoint returns Pinnacle + 40 retail books in one call; 500 free req/month is enough for a daily picks run |
| Sharp-line math | `american_to_prob` + `remove_vig` | Converting American odds to implied probability then normalizing removes the bookmaker overround in O(n) |
| API | FastAPI | Async lifespan manages shared httpx client and graph instance |
| Database | PostgreSQL + SQLAlchemy | Pick history and CLV tracking; `PostgresSaver` for LangGraph checkpoints |
| Payments | Stripe webhooks | Subscription lifecycle via `customer.subscription.created/deleted` events |
| Testing | pytest + respx | respx mocks at the httpx transport layer; no network calls in CI |
| Observability | LangSmith | Traces each graph run at node level; HITL pause and resume appear as two linked traces (`picks/...` then `approve/...`), making the two-phase architecture visible without reading code |

---

## Closing Line Value (CLV)

CLV compares the price you took against the sharp book's closing price:

```
CLV = no-vig closing probability - implied probability of the price you bet
```

A positive CLV means you beat the close. Sportsbooks use CLV to identify sharp bettors and limit their accounts. The comparison is against the price actually taken, not the model's estimate: if Pinnacle closes at a no-vig 52.2% and you bet at -108 (implied 51.9%), you have +0.3% CLV whatever the model believed.

The settlement job captures closing lines:

```bash
python -m steambot settle --window-minutes 30
```

It finds picks with no `closing_price` whose game starts within the window, pulls the current Pinnacle market, devigs it, and writes `closing_price`, `closing_probability`, and `clv`. Run it near kickoff (cron a few minutes before the day's first game). Then `SELECT AVG(clv) FROM picks WHERE clv IS NOT NULL` shows whether picks are consistently ahead of the market.

---

## Getting started

```bash
cp .env.example .env
# fill in ANTHROPIC_API_KEY, ODDS_API_KEY, STRIPE_SECRET_KEY, STRIPE_WEBHOOK_SECRET
docker compose up
```

API is available at `http://localhost:8000`. The `/health` endpoint confirms the service is running.

Without `DATABASE_URL` the service still runs picks end to end but skips persistence, logging a warning at boot. Set `STEAMBOT_ENV=production` to turn that fallback into a boot failure; a deployment that silently drops pick history has no CLV record.

**Start a picks run:**

```bash
curl -s -X POST http://localhost:8000/api/runs \
  -H "Content-Type: application/json" \
  -d '{"sport": "americanfootball_nfl", "user_id": "demo"}' | jq
```

**Approve candidates:**

```bash
curl -s -X POST http://localhost:8000/api/runs/{run_id}/approve \
  -H "Content-Type: application/json" \
  -d '{"approved_pick_ids": ["pick-uuid-1", "pick-uuid-2"], "user_id": "demo"}' | jq
```

### Tracing

Add your LangSmith key to `.env` to enable run tracing:

```bash
LANGCHAIN_API_KEY=lsv2_pt_...
LANGCHAIN_TRACING_V2=true
LANGCHAIN_PROJECT=steambot
```

Each picks run produces two traces in the LangSmith UI:

- `picks/americanfootball_nfl/2026-01-15` -- covers odds fetch, fair-line derivation, Claude pick generation, and the HITL pause
- `approve/<run_id[:8]>` -- covers the resume and pick persistence

Tracing is optional. The service starts and runs normally without `LANGCHAIN_API_KEY` set.

**Run tests (no API keys needed):**

```bash
pip install -e ".[dev]"
pytest -q
```

---

## Eval harness

`eval/dataset.jsonl` contains 18 golden test cases that verify the deterministic math layer independently of the LLM. Run without API keys:

```bash
pip install -e ".[dev]"
python -m eval
```

Output:
```
vig_removal          5/5  [#####]
ev_calculation       4/4  [####]
clv_calculation      3/3  [###]
edge_filter          3/3  [###]
structural           3/3  [###]

Total: 18/18 passed  pass rate: 100.0%
```

To write a JSON report:

```bash
python -m eval --out eval/report.json
```

**What is tested:** vig removal accuracy (symmetric and asymmetric markets), EV formula correctness for favorites and underdogs, CLV sign convention (positive = beat the closing line), edge filter threshold compliance, and pick structural validity (required fields, confidence enum, non-negative edge). The harness imports directly from `steambot.state` so any change to the production math functions fails the eval immediately.

---

## Known limitations

1. **Rate limiting is per-instance.** There is no shared Redis counter across multiple app replicas. Fine for the current demo scale; documented trade-off.
2. **Off-season returns empty.** The Odds API returns no NFL games May through July. The `/api/runs` endpoint returns an empty `candidates` list rather than an error, which is correct but may confuse first-time callers.
3. **Closing line is a near-kickoff snapshot, not the true close.** The free Odds API tier has no historical endpoint, so `python -m steambot settle` records whatever Pinnacle shows when it runs. If the job does not run inside its window before kickoff, `clv` stays `null` for those picks; there is no backfill. Line movement in the final seconds before kickoff is also invisible to a snapshot taken minutes earlier.
4. **CLV ignores point drift.** A spread bet at -3.5 that closes at -4.0 is compared by price only; the half-point of line movement is directional evidence the price comparison understates.
5. **No authentication.** The `user_id` field is caller-supplied with no JWT verification. Adding auth is the first production-readiness gap.
6. **MemorySaver in tests.** The graph uses `MemorySaver` (in-process) for local dev. Production requires `PostgresSaver` for checkpoints to survive restarts; the switchover is a one-line change in `graph.py`.
