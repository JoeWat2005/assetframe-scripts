---
name: mvp
description: Generate an AssetFrame Snapshot (free) + AssetFrame Pro (paid) report pair for any instrument — website-ready PDFs/HTML with canonical-data QA, price ladder, session rules, claim gating, and ledger-registered predictions. Triggers on "/mvp X", "assetframe report on X".
---

# AssetFrame MVP Report Pipeline (/mvp INSTRUMENT) — Engine V2

Brand: **AssetFrame** — *"Next-session market intelligence, scored after the fact."*
Products: **AssetFrame Snapshot** (free, 1 page) and **AssetFrame Pro** (paid, 3–6 pages).
Positioning: general market research and decision support — never personal advice, never guaranteed, no execution. Logo (`logo/logo_trimmed.png`, wordmark "Asset"+boxed "Frame") appears on every PDF header, HTML export, and preview. Report IDs `AF-YYYYMMDD-<INSTRUMENT>`.

Output per run: `reports/YYYY-MM-DD/<INSTRUMENT>/` → `free.pdf`, `pro.pdf`, `free.html`, `pro.html`, `metadata.json`, `preview.png`.

## What changed in V2 (read this first)

AssetFrame is an **agentic research-and-publishing system**, not a deterministic quant generator. V2 removes the fragile, expensive part of the old flow — hand-building 23–40 KB canonical payloads + a hidden `*_anchored.json` + a predictions file per instrument — **without removing the analyst**.

> **Core principle: do NOT automate away the analyst. Automate away fragile manual JSON construction.**
>
> - **AI = analyst + strategist + research desk.** You author exactly ONE artifact, `data/briefs/<NAME>_research_brief.json`: directional view, primary thesis, prediction *intent* (type + expected move + reasoning, never prices), scenarios, preferred setup, invalidation logic, risk assessment, conviction reasoning, news + social *interpretation*, and "why the call matters."
> - **Python = compiler + validator + quantitative engine.** Every number, level, pivot, band, R:R, ladder, prediction, window, and integrity check is generated and validated by Python. You never type a price.
> - **Ledger = memory + calibration + proof.** The append-only ledger is now an *input* (via `ledger_context.py`, and later `research_memory.py`) as well as the scored-after-the-fact record — under a hard no-look-ahead rule.
> - **Confidence = deterministic + auditable.** `confidence.py` computes the score; you **explain** it, you never set it.
> - **Social = optional + subtract-only.** The pipeline runs normally with no social data; social may only *reduce* confidence, never raise it, and is never a factual source.
> - **Human = final reviewer.** A report is generated, QA-passed, then **reviewed by a human before publish**.

Concretely vs v1.x: the old "hand-author the canonical payload" step is gone; the old "register predictions" step is folded into the scaffold; and the manual authoring traps (the `~-N` R:R lint trap, hand-declaring every level, hand-writing the confidence scorecard) are gone because the scaffold and confidence engine build all of that.

## Language and banned-wording rules (unchanged, still enforced)

Use Research view / Long-biased scenario / Short-biased scenario / Conditional setup / Invalidation / No-trade condition / Scenario / General market research / Not personal advice. NEVER write: you should buy/sell, sure trade, risk-free, easy profit. "Guaranteed" and "personal recommendation" are allowed ONLY in negated compliance form ("No outcome is guaranteed", "not a personal recommendation") — the QA gate checks the preceding context. R:R is rendered by the engine as "T1 1.5x; T2 2.1x" / "T1 below 1.0x; T2 1.4x" / "No valid R:R - excluded"; you never type R:R. The generator's QA gate hard-fails the build on violations. The Free teaser and disclaimer fields are exempt from the free-split content scan (the teaser legitimately NAMES Pro features); everything else in the free tier is scanned — and the scaffold's `_assert_free_split` also rejects pro-only vocabulary (r:r, entry zone, invalidation, t1/t2, ladder, source audit, outcome ledger, hedging, risk math) leaking into the brief's `free_*` fields, so keep the Snapshot prose plain.

---

## The V2 flow (per instrument)

```
score_report.py (score expired windows FIRST)
  -> intraday.py [--anchor live|prior-completed|friday]
  -> research_pack.py
  -> social_pack.py            (OPTIONAL — pipeline runs without it)
  -> ledger_context.py
  -> AI writes data/briefs/<NAME>_research_brief.json   (the ONLY hand-authored artifact)
  -> scaffold_payload.py       (compiles payload + predictions; invokes confidence.py; rejects unsupported numbers/claims)
  -> mvp_report.py             (QA gate; aborts on error)
  -> HUMAN REVIEW
  -> publish.py / export_content.py / sync-db
```

Engine artifacts (`data/analysis/`, `data/research/`, `data/social/`, `data/ledger_context/`, `data/briefs/`, `data/payloads/`, `data/predictions/`) stay gitignored; only `web/content/*.json` + the new JSON DB columns reach git/Neon.

### 1. Score expired ledger windows first (no look-ahead)

Same as advisor Step 1.5. Check `data/predictions/*_predictions.json` (and any `agent-advice/exports/tables/*_predictions.json`) for a passed `window_end_utc` with no matching `ledger/outcome_ledger.csv` row; refresh that instrument's hourly CSV; resolve `manual` predictions via WebSearch (leave genuinely unresolvable ones MANUAL with a stated reason); run:

```
python scripts/score_report.py <predictions.json> [--hourly <csv>] [--manual P5=Y[,P6=NT]] [--dry-run] [--force]
```

Verdicts are Y / N / NT / MANUAL; hit rate counts Y / (Y + N). The ledger is **append-only** — never edit or reorder rows, never score an incomplete window (`--force` only for a deliberate PARTIAL or an early-close CSV). The first 13 columns are the original schema; V2 added **additive trailing columns** `conf_version, conf_raw, asset_class, pred_type, direction, horizon, market_regime` (older rows just read these back as `""`). The calibration block appears at ≥10 rows (buckets `<=60` / `61-75` / `>75`). Doing this first is what keeps `ledger_context` and `research_memory` provably free of look-ahead.

After scoring, regenerate the calibration map so the latest outcomes inform confidence:

```
python scripts/calibrate.py [--ledger ledger/outcome_ledger.csv] [--out ledger/calibration_map.json] [--dry-run]
```

`calibrate.py` fits a weighted isotonic regression of realized hit-rate on the ledger's `conf_raw` (the pre-calibration capped score; falls back to `confidence` for legacy rows), filtered to the current `conf_version`, then **shrinks toward identity** with `w = min(1, n_rows / 40)`. So below ~10 rows the map is essentially identity (published == raw) — it earns its adjustment only as the ledger fills. Exit 0 always; an empty/young ledger writes a valid identity map.

### 2. Run the engine (with the right anchor)

```
python scripts/intraday.py <YAHOO_SYMBOL> --name <PREFIX> --hrange 10d --related "..." [--anchor live|prior-completed|friday]
```

`--anchor` re-derives floor pivots + ATR day-bands on a **chosen completed daily session** instead of the live/in-progress one — this **replaces the old hand-built `*_anchored.json`**:
- `live` (default) — pivots from the prior completed session, bands anchored on TODAY'S session open.
- `prior-completed` — pivots from the last COMPLETED daily session's HLC, bands anchored on that session's CLOSE (even if a live session is forming) — the normal pre-market case.
- `friday` — like `prior-completed` but the most recent completed Friday session (weekend / Monday pre-market); falls back to last completed if none.

When `--anchor` != `live`, `pivots_classic` / `atr_day_bands` are OVERWRITTEN with the anchored values (so `scaffold_payload.py` consumes them transparently), the live values are preserved under `pivots_classic_live` / `atr_day_bands_live`, and an `anchor` block records the choice. Output is `data/analysis/<PREFIX>_analysis.json` with CSVs under `data/candles/`. Always read `freshness`, `degraded`, `provider`, and `windows` before trusting any number. Pick `--related` for the asset class (include `^VIX,^VVIX,^VIX3M` for equity indices — they feed the options-context section). Single stocks/ETFs run WITHOUT `--roll-utc`; futures/FX/crypto use `--roll-utc 22`.

### 3. Build the research pack (sourcing layer)

```
python scripts/research_pack.py <NAME> [...]    # see the script's --help
```

Role: gather and **source** the factual context into `data/research/<NAME>_research_pack.json` — macro news, asset-specific news, earnings/events, economic calendar with exact UK times, regulatory + geopolitical context — each item carrying a source URL, timestamp, and source-quality note, plus a `source_gaps[]` list. Built from WebSearch/WebFetch + official calendars (official sources first). **Rule:** the AI may *interpret* news but never invent it; every factual claim in your brief must trace to a source in this pack, and the QA gate fails unsupported high-impact claims. The pack is what `scaffold_payload.py` and `confidence.py` check the brief's `claims[]` against. (Script is new per the V2 plan — confirm the exact CLI with `--help`.)

### 4. Build the social pack (OPTIONAL, subtract-only)

```
python scripts/social_pack.py <NAME> [...]      # OPTIONAL; see the script's --help
```

Role: summarise the *market conversation* into `data/social/<NAME>_social_pack.json` — sentiment summaries, crowding indicators, notable discussions, emerging narratives, source references, a signal-quality score, and explicit hype/manipulation warnings (the `aggregate` block carries `hype_risk`, `crowding_risk`, `contrarian_warning`, which the confidence engine reads). Sourced via the `last30days` skill (Reddit/HN/Polymarket keyless) + WebSearch.

**This step is optional and the pipeline must run normally if it is skipped, absent, or empty.** Use social for sentiment awareness, catalyst discovery, crowding risk, contrarian warnings, and retail-attention shifts. NEVER use it for factual claims, for generating confidence, or to override price / ledger / sourced news. Social is supplementary, non-authoritative, and may only *reduce* confidence — never independently raise it. Always label it "market conversation," never fact. (Script is new per the V2 plan — confirm the exact CLI with `--help`.)

### 5. Build the ledger context (ledger as INPUT)

```
python scripts/ledger_context.py <NAME> [--ticker T] [--asset-class equity] [--as-of "YYYY-MM-DD HH:MM"] [--recent-k 8] [--print]
```

Writes `data/ledger_context/<NAME>_ledger_context.json`: instrument hit rate, asset-class hit rate, prediction-type hit rates + counts, recent streaks, recent drift, similar-setup history, known success/failure patterns, and `notes_for_ai[]`. **Hard rule — no look-ahead:** it aggregates ONLY rows whose `window_end_utc` is strictly before `--as-of` (default: now). It degrades gracefully — an empty or young ledger yields a valid "no history yet" (neutral) context, so day-one runs work. You receive this BEFORE writing the brief and may adjust conviction, scenario/catalyst weighting, or setup preference from history — e.g. "similar upside breakouts here recently underperformed → keep the thesis, cut conviction." The same file feeds `confidence.ledger_confidence`. (Future: `research_memory.py` derives `ledger/research_memory.json` — thesis themes, catalyst types, regimes, reasoning patterns vs outcome quality — surfaced through `ledger_context.py` under the same no-look-ahead rule; see that script's `--help` when it lands.)

### 6. Write the research brief (the ONLY hand-authored artifact)

Author `data/briefs/<NAME>_research_brief.json` — small, prose + intent + sourced claims, **NEVER prices, levels, R:R, ladders, or confidence numbers**. This is your analyst output; everything else is compiled from it. The scaffold rejects a brief whose `claims[]` aren't sourced or whose intent references prices not in the engine's level set. See the brief schema below.

When you write it, you have in front of you: the engine analysis, the research pack, the (optional) social pack, and the ledger context. Make the directional call, frame the prediction *type* and expected move in words, write the scenarios and risks, interpret news and social, and reason about conviction (including any ledger-driven adjustment). Tag the primary prediction with a taxonomy `type` (see Prediction taxonomy below).

### 7. Compile the payload + predictions (scaffold)

```
python scripts/scaffold_payload.py <NAME> \
  [--analysis data/analysis/<NAME>_analysis.json] \
  [--brief data/briefs/<NAME>_research_brief.json] \
  [--research data/research/<NAME>_research_pack.json] \
  [--social data/social/<NAME>_social_pack.json] \
  [--ledger-context data/ledger_context/<NAME>_ledger_context.json] \
  [--calib ledger/calibration_map.json] \
  --session-profile <profile> \
  [--out data/payloads/<NAME>_af_payload.json] \
  [--predictions data/predictions/<NAME>_predictions.json] [--check]
```

`scaffold_payload.py` is the compiler/validator. It:
- **builds canonical levels** from the engine analysis (pivots / ATR bands / swing highs+lows) via a fixed id+`cls` catalog (ids like `r2, tail_hi, swing_hi, r1, inner_hi, pp, anchor, s1, inner_lo, swing_lo, s2, tail_lo`; classes ∈ tail|resistance|target|support|entry|invalidation), de-duped by value and sorted high→low. Every price that appears anywhere lives ONCE here;
- **builds the long + short conditional setups**, with entry/invalidation/T1/T2 picked by reference to level *values* (never free-typed, so they cannot drift), and **computes R:R** at the zone-edge trigger, formatted to `mvp_report`'s `RR_OK`;
- **builds the ladder and `ledger_levels`** from those levels (every prediction reference price is guaranteed to be a canonical level — including manual P6 reference prices like the anchor/last close);
- **reads `canonical.last_price`** straight from the hourly CSV's last close, so the price triple-equality (CSV == canonical == header) holds *by construction*;
- **emits the predictions file** `data/predictions/<NAME>_predictions.json` (P1..P6 falsifiable predictions mapped onto canonical ids + a `setup` block + the taxonomy block) — this **folds in the old "register predictions" step**. The payload and predictions are written from one source, so they cannot diverge; `payload.confidence == predictions.confidence` always;
- **invokes `confidence.py`** (Step 8) and writes the same published int into both files;
- **rejects** prices not in `canonical.levels`, predictions not bound to a canonical id, claims with an invalid status, and any `used_in_thesis` claim whose status is unverified/stale/unavailable.

The narrative (theses, scenarios, risks, market-summary bullets, long/short view, stats prose) comes from the brief; the numbers and structure come from here. `--check` validates the brief + would-be payload and prints the would-be confidence **without writing**. Exit 2 on a brief/validation error. Use `--check` first, fix the brief, then run for real.

Session profiles (`--session-profile`, passed to `sessions.get_session`, or set `session_profile` in the brief): `cme_futures`, `fx_spot`, `crypto_24_7`, `us_equity_rth`. For single stocks/ETFs use `us_equity_rth` (pre-market 08:00-13:30 UTC / regular 13:30-20:00 UTC / after-hours 20:00-00:00 UTC EDT; the window targets the NEXT REGULAR session; tradable levels are regular-session unadjusted prices; stock tone — Nasdaq session/pre-market/after-hours/earnings window/gap risk, never Globex/roll/maintenance language). The scaffold copies the session's window + state fields into `meta.*` and its prose into the Pro "Asset-session rules" section. Web-verify exchange holidays when within a week of one. The window is the next full session when <90 min remain (<240 min on Fridays); rolling 24h for crypto — never say "market close" for crypto.

### 8. Confidence engine (deterministic; you explain it, you never set it)

`confidence.py` is invoked **by the scaffold** — you do not run it by hand for a report (its `__main__` is a demo). It computes:

```
raw       = 50*market + 30*ledger + 20*catalyst + social_adjustment   (components 0..1; social_adj -10..0)
capped    = min(raw, <hard caps>)
published = calibrate(capped)                                          (isotonic map; identity early)
```

The four blocks (default preset — tunable; calibration is the ultimate ground truth):
1. **Market** (analysis + setup): trend alignment, momentum (hourly/daily RSI14, MACD cross + histogram delta), structure/entry confluence, R:R quality (T1 ≥ 1.5x rewarded), asset-relative volatility normality, and measured data quality (`compute_dq`, which replaces the old hand-set `data_quality_score`).
2. **Ledger** (from `ledger_context`): realized hit rate for this prediction type / instrument / asset class, Bayesian-shrunk toward 0.5 by sample size, plus streak/calibration history.
3. **Catalyst** (from brief + research pack): claim support, source quality, source gaps; a `used_in_thesis` claim whose source isn't in the research pack is downgraded.
4. **Social adjustment** (from social pack; OPTIONAL): crowding/hype/contrarian penalties — **subtract-only**, 0 if no social data.

**Hard caps (take the min):** stale data → 40 · degraded data → 50 · single-source/unverified high-impact thesis → 55 · hype-driven social thesis → 55 · ledger strong historical failure pattern → 55 · cold indicators (SMAs not warm at display start) → 60 · engine errors → 65. The result carries `components[]` (for the Pro scorecard), `caps_applied[]`, `raw`, `capped`, `published`, `band`, `calibrated`, and `conf_version`, so the published number is fully explainable. **Your job is to explain that score in prose** (why the call is or isn't strong) — never to invent or override it.

> Mixed-regime note: historical ledger rows keep their old freehand hand-scores; `conf_version` lets `calibrate.py` filter to the new engine. Until enough V2 rows accumulate, calibration is near-identity and buckets may show little signal — say so rather than over-reading the calibration block.

### 9. Prediction taxonomy (cross-cutting)

Every report's primary prediction is tagged with a `prediction_type` ∈ **breakout | rejection | continuation | mean_reversion | range_hold | volatility_expansion**, plus `direction` (bullish/bearish/neutral/mixed), `horizon` (intraday/next_session/multi_session), `asset_class` (equity/crypto/fx/futures/index/commodity), and `market_regime` (trend_up/trend_down/range/choppy/high_volatility/low_volatility/breakout). `taxonomy.py` validates these (a typo raises `TaxonomyError` before anything freezes into the append-only ledger), derives `asset_class` from the session profile (refining generic futures into index/commodity), and derives/normalizes `market_regime` from the engine when your free-text doesn't match a label. This taxonomy **flows through** predictions → ledger → track record → confidence → calibration → research memory, enabling insights like "breakout predictions in high-volatility crypto regimes hit 71%." Note: the taxonomy `prediction_type` is the *strategic archetype* of the call (one per report); it is distinct from the per-prediction *scoring mechanic* in `score_report.py` (`close_above`, `range_inside`, `touches`, `no_close_below`, …), which is unchanged.

### 10. Generate the reports + QA gate (build aborts on failure)

```
python scripts/mvp_report.py <payload.json|out_dir> [...]    # see the script's --help
```

`mvp_report.py` renders the **Snapshot** (free) and **Pro** PDFs + HTML + `metadata.json` + `preview.png`, and runs the QA gate. Most V2 identity checks now pass **by construction** (the scaffold built them) but remain as regression guards: price triple-equality, levels↔setups↔ladder↔ledger identity, R:R lint, banned-language scan, free/pro split, timestamps UTC-normalized, no lookahead, session fields present, logo present. **V2-specific QA:** brief `claims[]` must trace to the research pack (unsupported high-impact → fail via `THESIS_BLOCKED`); social must be labelled "market conversation," not fact; `primary_prediction.type` ∈ the taxonomy enum; predictions reference only canonical ids. The Pro confidence gauge + scorecard consume the computed confidence breakdown (component table + caps applied + calibration note).

Report contents are unchanged from v1.x:
- **Snapshot (free):** logo, instrument+ticker+asset class, timestamp+TZ, risk window, status & risk badges, last price + bar time, broad expected range, basic data quality, ONE chart (≤3 labelled levels, no pivots/bands), 3 bullets (core thesis / main catalyst / main risk), simplified Bull/Base/Bear matrix, visual timeline strip, Pro teaser, short disclaimer. EXCLUDED (QA-enforced): entries, invalidation logic, R:R, sizing math, options ideas, scorecard, audit, ledger, price ladder.
- **Pro (paid):** executive header (+ verdict box), daily regime chart, intraday chart, RSI, **price ladder** (upper tail → R2 → T2 → T1 → trigger → last → entry zone → support → invalidation → lower tail; canonical levels only), confidence gauge, then Market summary (+cross-asset table), **Long / Short Research View**, Scenario matrix, Event-risk timeline, Technicals & key levels (distance + classification), Conditional setups, **Options / Hedging Context** (data-gated; VIX/VVIX/term-structure & implied-move-vs-ATR for indices; venue IV/funding/basis for crypto if sourced; otherwise exactly "No options context included: reliable IV/skew data unavailable." — never construct option trades), **Asset-Specific Statistics** (per-class menu below; every stat sourced + timestamped; no filler), "What can go wrong?", generic contract/risk math (educational, sizing-depends-on-circumstances line), Trade-quality scorecard (from the confidence breakdown), **Outcome ledger** (registered predictions; "Ledger starts here." if empty; Free may MENTION scoring but never shows the ledger), full Source audit, Asset-session rules, footer on every page.

### 11. HUMAN REVIEW (mandatory, before publish)

A report is **never published straight from the generator.** After QA passes, do a visual + editorial inspection — Read `free.pdf`, `pro.pdf`, `preview.png` (spot-check the HTML) page by page against: logo present, no placeholder branding, no overlap/clipping/cramping, separate status & risk badges, no annotation collisions, simple Free chart, ladder present in Pro and matching the tables, readable audit + setups, unambiguous R:R, warmed indicators, session rules applied, options context omitted where unsupported, claims gated, confidence explanation matches the computed score, correct paths — then stamp:

```
python scripts/mvp_report.py <out_dir> --stamp-visual
```

A human reviewer signs off before anything goes to the website. This is the final gate in the agentic system: AI drafts, Python validates, a human approves.

### 12. Publish / export / sync-db

Follow the existing AssetFrame publish workflow (see `mvp/CLAUDE.md` and the publish memory): export the report content to `web/content/`, upload report files to private R2, then sync the database. Typical order:

```
python scripts/export_content.py [...]        # -> web/content/*.json (track record, editions); see --help
python scripts/publish.py [--date YYYY-MM-DD] [--dry-run]   # upload free + Pro files to private R2 (auto-loads web/.env.local)
node web/scripts/sync-db.mjs                   # idempotent DELETE + re-insert of scored_results; apply to BOTH Neon branches
```

`out_dir` MUST be `reports/<date>/<slug>` (avoid the historical nesting bug). The ledger and `web/content/*.json` are the only things that reach git/Neon; everything under `data/` stays local/gitignored.

---

## The research brief schema (`data/briefs/<NAME>_research_brief.json`)

This is the ONE artifact you author. Prose + intent + sourced claims only — **no prices, levels, R:R, ladders, or confidence numbers** (the scaffold builds those and rejects the brief if you slip a price into the intent). Required: `status`, `risk`, and a session profile (`session_profile` here or `--session-profile` on the scaffold). Fields below are taken from the working `AAPL_research_brief.json` example.

**Identity / framing**
- `name`, `ticker`, `instrument`, `asset_class_label`, `asset_class_key` (taxonomy enum), `session_profile`, `venue`.
- `status` (e.g. Wait/Long-biased/Short-biased), `risk` (Low/Medium/High), `directional_view` (taxonomy direction), `horizon` (taxonomy horizon), `market_regime` (free text — normalized against the engine).
- `primary_bias`, `research_view` (one-line stance), `long_scenario_quality` / `short_scenario_quality` ∈ {High quality, Acceptable, Low quality, Management only, No-trade}.

**Prediction intent (no prices)**
- `primary_prediction`: `{type` (taxonomy enum), `expected_move` (words), `time_horizon`, `reasoning`, `invalidators[]` (described, not priced)`}`.
- `alternative_prediction`: `{type, reasoning}`.
- `preferred_setup`: `{side` ∈ long/short/wait, `why_this_setup`, `avoid_if}`.
- `manual_prediction`: optional prose for a P6 manual prediction (resolved later via `--manual`); the scaffold binds its reference price to the anchor level.

**Narrative + context (interpretation, sourced)**
- `exec_summary`, `verdict` `{line, best, risk, stand_aside}` (conditional sentence, never an instruction).
- `catalyst_status`, `next_major_event`, `cross_check`.
- `options_context_included` (bool) + `options_context_reason`; `source_gaps[]`; `asset_specific_stats_included[]`.
- `claims[]`: each `{claim, status` ∈ confirmed/multiple-source/single-source/unverified/stale/unavailable, `source, used_in_thesis}`. **Rule:** unverified/stale/unavailable claims must NOT be `used_in_thesis` (scaffold hard-fails); single-source may support but not centre a thesis; every `used_in_thesis` claim must trace to the research pack. Never overstate (write "multiple-source reports of a draft agreement; signature unconfirmed", not "confirmed draft").
- `catalysts[]`: `{when, label, in_window, gap_risk, relevance}`.
- `risks[]`: short prose risk bullets.
- `scenario_matrix[]`: `{case, trigger, move, invalidation, confidence` (word band)`, watch}`.
- `narrative`: `{free_bullets[]{label,text}` (plain, no pro vocabulary)`, free_scenarios[]{scenario,trigger,move,watch}, market_summary[]{label,text}` (labels: Technical thesis / Macro-catalyst thesis / Cross-asset read / What would prove this wrong / Timing risk; author with `<br>` after the bold label), `long_short_view` (HTML `<ul>`), `technicals_note, stats_html` (sourced table)`}`.

Optional overrides (the scaffold has sane defaults): `price_source`, `price_type`, `contract_month`, `cross_check`, `last_price_note`, `free_last_price`, `free_expected_range`, `free_chart_label`, `timeline_events[]`, `exec[]`, `source_confidence`, `title`, `subtitle`, `roll_utc`.

---

## Editorial polish (premium standard — unchanged)

- **Market summary**: real bullets with the bold label on its OWN line (`<li><b>Technical thesis</b><br>Short sharp sentences…</li>`). Labels: Technical thesis / Macro-catalyst thesis / Cross-asset read / What would prove this wrong / Timing risk. No bullet over ~3 rendered lines; skimmable in under 20 seconds; institutional tone, never promotional.
- **Pro verdict box** (mandatory, from `verdict`): one concise conditional sentence, then Best opportunity / Main risk / Stand-aside condition. Never an instruction.
- **Source confidence box + Report quality card** (rendered before the Source audit): from `source_confidence` and the computed data-quality/QA lines; both mirror into metadata.
- **"Why it matters" micro-explanations**: a short professional phrase per stats/cross-asset row (e.g. "VIX lower = less demand for downside protection"). Never lectures.
- **Tables**: commentary below tables, short cell text, premium pagination (a section never starts in the bottom ~34mm of a page). Pro may run 4–7 pages — readable beats compact.
- **Price ladder**: short labels, consistent decimals, auto-caption "Levels are conditional research references, not trade instructions."

## High-impact claim gating (unchanged philosophy; now enforced against the research pack)

IPO/debut, market-cap, index inclusion/exclusion, central-bank probabilities, geopolitical deals, official inventories, earnings dates, major corporate/analyst/ratings news, CFTC positioning, exchange schedule changes, roll assumptions, options/gamma, ETF flows → each labelled **confirmed / multiple-source / single-source / unverified / stale / unavailable** in the brief's `claims[]` with source and `used_in_thesis`. Confirmed and multiple-source claims may drive thesis; single-source may support it (labelled, never central); **unverified/stale/unavailable must NOT drive thesis** (the scaffold hard-fails, and `mvp_report.py` re-checks against the research pack via `THESIS_BLOCKED`). Market-moving single-source claims reduce the catalyst-confidence component automatically.

## Asset-specific statistics menus (include only what is sourced; no filler)

- **Indices/futures**: cash-vs-futures distinction, fair value (if available), VIX/VVIX/term structure, breadth (A/D, % above DMAs — usually unavailable: say so), sector leadership, put/call & gamma (if sourced), cash open/close + futures close, overnight gap risk, contract roll status.
- **Oil/energy**: WTI/Brent distinction + spread, front month + roll status, API/EIA timings + inventories, crack spreads, refinery utilisation, OPEC+ headlines, Baker Hughes, CFTC positioning, geopolitical supply risk, settlement/maintenance.
- **Metals**: futures-spot basis, DXY, real/nominal yields, Fed path, central-bank demand (if sourced), haven context, gold/silver ratio, silver industrial demand, CFTC positioning, COMEX inventory/OI (if available).
- **FX**: session H/L by Asia/London/NY, daily/weekly ATR, rate differential, central-bank pricing, CFTC positioning, IV/risk-reversals (if available), rollover window, weekend gap, intervention risk, macro calendar.
- **Crypto**: 24h/7d range, funding rate, open interest, perp basis, liquidation clusters (if sourced), ETF flows (if available), BTC dominance, stablecoin liquidity, venue/outage risk, weekend liquidity, funding windows.
- **Stocks/ETFs**: prev close, OHLC + gap %, volume vs 20d, ATR percentile, VWAP, RS vs sector, earnings date/timing + implied move, short interest, options IV/put-call, S/R distance, pre/after-hours separation.
- **Bonds/rates**: yield change, curve slope, futures/cash distinction, auctions, inflation/employment data, CB pricing, real yields, term premium (if sourced).

## Shared infrastructure

`scripts/intraday.py` (warm-up-extended data, `windows` block, `--anchor`, shared `compute_pivots_bands()`) · `scripts/taxonomy.py` (the one prediction vocabulary + validators) · `scripts/research_pack.py` (sourced factual context) · `scripts/social_pack.py` (optional market-conversation signal) · `scripts/ledger_context.py` (ledger-as-input, no look-ahead) · `scripts/scaffold_payload.py` (payload + predictions compiler/validator) · `scripts/confidence.py` (deterministic confidence) · `scripts/calibrate.py` (isotonic calibration map) · `scripts/report_pdf.py` (charts with warm-crop + partial-indicator disclosure) · `scripts/sessions.py` (session profiles + window policy) · `scripts/mvp_report.py` (generator + QA gate + `--stamp-visual`) · `scripts/score_report.py` (append-only ledger with additive taxonomy/conf columns, `--manual`, `--dry-run`, calibration) · `scripts/research_memory.py` (reasoning-level learning; future) · `scripts/social_posts.py` (safe-worded distribution drafts, no auto-posting; future) · `scripts/publish.py` / `export_content.py` / `web/scripts/sync-db.mjs` (publish workflow). Warm-up minimums: daily 1y display ← 2y fetch; hourly 10d display ← 31d fetch; partial lines hidden or labelled; never infer trend from cold indicators. Where a flag is uncertain, run the script's `--help`.

## Changelog

- **2.0 (2026-06-16, Engine V2)**: agentic engine + ledger memory + deterministic confidence. The analyst now authors **one** artifact — `data/briefs/<NAME>_research_brief.json` (prose + intent + sourced claims, never prices) — and Python compiles everything numerical/structural. New steps: `research_pack.py` (sourced context), optional subtract-only `social_pack.py`, `ledger_context.py` (ledger-as-input under a hard no-look-ahead rule), `scaffold_payload.py` (builds canonical levels/setups/ladder/RR/ledger_levels AND the predictions file from one source — folds in the old "register predictions" step), `confidence.py` (deterministic Market 50 / Ledger 30 / Catalyst 20 blend + subtract-only social + hard caps + isotonic calibration; the AI **explains**, never sets it), `calibrate.py` (shrinkage-to-identity isotonic map filtered by `conf_version`), `taxonomy.py` (breakout/rejection/continuation/mean_reversion/range_hold/volatility_expansion + direction/horizon/asset_class/market_regime, threaded predictions→ledger→track-record→confidence→calibration), `intraday.py --anchor live|prior-completed|friday` (replaces the hand-built `*_anchored.json`), and an explicit **HUMAN REVIEW** step before publish. **Deleted** the v1 "hand-author the canonical payload" step, the manual "register predictions" step, the `~-N` R:R authoring trap, manual level-declaration rules, and hand-writing the confidence scorecard. `score_report.py` gained additive ledger columns (`conf_version, conf_raw, asset_class, pred_type, direction, horizon, market_regime`); `compute_dq()` replaces the hand-set `data_quality_score`. Safety, disclaimer, claim-gating, and data-source rules unchanged. Output path moved to `reports/YYYY-MM-DD/<INSTRUMENT>/`.
- **1.2 (2026-06-12, polish pass)**: premium editorial standard — label-break Market-summary bullets (`<br>` support), Pro verdict box, Source confidence box + Report quality card (rendered before the Source audit, mirrored to metadata), expanded claim vocabulary (multiple-source/stale; never overstate to "confirmed"), why-it-matters micro-explanations, ladder caption, premium pagination (4-7 Pro pages allowed), `us_equity_rth` session profile + single-stock tone rules (first instrument: AAPL).
- **1.1 (2026-06-12, post-build)**: refinements proven on the five-instrument launch pass — negation-aware banned-language rule; teaser/disclaimer exemption from the free-split scan; `~-N` authoring trap documented; ledger references (incl. manual P6 prices) must be canonical levels; `<PREFIX>_af_predictions.json` non-clobbering registration when a prior window is unscored; sessions.py weekend-detection fix.
- **1.0 (2026-06-12)**: initial AssetFrame pipeline — logo branding, Snapshot/Pro split, price ladder, timeline strip, Long/Short Research View, options-context gating, asset stats menus, canonical data object + QA gate, session rules module, high-impact claim gating, HTML exports, metadata v2, /mvp flow.
