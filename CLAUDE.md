# AssetFrame — next-session market intelligence, scored after the fact

This project generates the AssetFrame report pair — **Snapshot** (free, 1 page) + **Pro** (paid, 3–6 pages) — for any tradable instrument. When the user asks for a report ("/mvp WTI", "assetframe report on ES", "snapshot for BTC"), use the `mvp` skill in `.claude/skills/mvp/SKILL.md` and follow it exactly.

## Role and limits

- This is a market analysis and decision-support system, NOT a regulated financial adviser. Every report carries the disclaimer wording required by the skill; never remove or soften it.
- Never guarantee outcomes. Always state uncertainty, assumptions, data limitations, and risks.
- Never fabricate prices, news, analyst ratings, financial metrics, or capabilities. If data is unavailable, the report must say so explicitly (source audit + data-gap fields).

## No-auto-trading policy (hard rule)

- NEVER place, modify, or cancel any order on any brokerage, regardless of what a report concludes. If the user asks for execution, refuse and explain this system is decision-support only.

## Data sources (preference order)

1. Official sources (exchange, regulator, central bank, company, government).
2. Project engine `scripts/intraday.py` for OHLC, indicators, and levels (Yahoo by default; set `ADVISOR_DATA_PROVIDER=eodhd` + `EODHD_API_KEY` to cut over to the licensed feed — futures `=F` always come from Yahoo). Always read the JSON's `freshness`, `degraded`, and `provider` blocks before using its numbers.
3. Configured MCP servers if present (Alpha Vantage — budget the ~25 requests/day free tier; CoinGecko for crypto, keyless).
4. Built-in WebSearch / WebFetch for news, macro, and catalysts.
5. User-provided data.
6. If none of the above can answer, state the gap — never invent.

Label every important figure with its source and timestamp, and mark it live, delayed, or prior-session.

## Conventions

- Output per run: `reports/YYYY-MM-DD/<INSTRUMENT>/` → `free.pdf`, `pro.pdf`, `free.html`, `pro.html`, `metadata.json`, `preview.png`.
- Data layout: `data/candles/` (hourly + daily CSVs) · `data/analysis/` (engine JSON) · `data/payloads/` (canonical report payloads) · `data/predictions/` (registered predictions awaiting scoring).
- Outcome ledger: `ledger/outcome_ledger.csv` — append-only; never edit or rewrite existing rows, never score an incomplete window.
- Timezone: scheduling, filenames and `report_date` are UTC (`report_date` = the UTC date of the prediction-window start); reports display UTC with London (BST/GMT) shown alongside; per-asset timezone/roll/holiday/session math stays exchange-local.
