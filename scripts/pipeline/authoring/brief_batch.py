"""brief_batch.py — author + critique research briefs through the Anthropic Message Batches API.

The synchronous path (brief_writer.py + critic.py, one subprocess per asset) bursts past the
per-minute token rate limit when several assets author at once, and pays full price. This module
submits ALL due assets' author calls as ONE batch (then all critic calls as a second batch):

  * No per-minute rate limit — batches have their own, far larger throughput pool. A 5am run with
    N assets no longer fails when N briefs would otherwise fire simultaneously.
  * 50% cheaper — all batch usage is billed at half the standard token price.
  * Scales to many assets at ~constant wall-clock — the asset count is just the batch size.
  * Briefs KEEP live web_search: server tools run their full agentic loop to completion inside the
    batch (pause_turn is handled server-side), so there is no research-quality trade-off.
  * Prompt caching stacks: the (identical) system prompts carry a cache breakpoint, so the rules
    block is written once and read at 0.1x across the batch (best-effort 30-98% hit rate).

Design notes:
  * Each batch request is ONE-SHOT — there is no mid-request client turn. The synchronous writer's
    single validation re-prompt therefore becomes a SECOND batch round: any brief that fails schema
    validation is re-authored once with its errors fed back as guidance (mirrors brief_writer's
    attempt-2 semantics). web_search pause/resume needs no such handling — the server completes it.
  * All request building + result parsing REUSES brief_writer / critic internals (SYSTEM_PROMPT,
    build_user_message, _extract_json, validate_brief, _news_settings, _verdict_errors) so the two
    paths stay in lock-step — a brief that passes here passes the scaffold's contract too.
  * Submission errors (network / auth) propagate to the caller so run_daily can fall back to the
    proven synchronous path; per-request failures (errored / expired / schema miss) are returned
    per-ticker so that asset degrades to needs_brief exactly as the sync path would.

Public API (consumed by run_daily.generate_due_batched):
  author_briefs(items, *, model, max_tokens)  -> {ticker: {"brief": dict|None, "telemetry": {}, "error": str|None}}
  review_briefs(items, *, model, max_tokens)  -> {ticker: verdict_dict|None}

`items` carry ALREADY-LOADED JSON dicts (the caller loads them):
  author item: {"ticker", "analysis", "memory_pack", "research"?, "social"?, "include_news"?}
  critic item: {"ticker", "brief", "analysis"?, "research"?}
"""
import os
import re
import time

import brief_writer as bw
import critic as cr
from anthropic_client import AnthropicBriefClient, resolve_prices

# Web-search tool (same as the synchronous writer). Server-side; runs to completion inside the batch.
_WEB_TOOL_TYPE = "web_search_20250305"
_DEFAULT_POLL_S = 15


class BatchTimeout(RuntimeError):
    """Raised when a batch does not reach 'ended' within the poll window (caller falls back)."""


def _batch_timeout_s():
    """Default TOTAL batch-poll budget (seconds) when the caller doesn't pass a shared deadline.
    run_daily passes an explicit deadline derived from the systemd run timeout, so this is mainly a
    fallback/test default. 2400s leaves room for a sync fallback inside the 5400s run timeout."""
    try:
        return max(60, int(os.environ.get("ASSETFRAME_BATCH_TIMEOUT_S", "2400")))
    except (TypeError, ValueError):
        return 2400


def _poll_s():
    try:
        return max(1, int(os.environ.get("ASSETFRAME_BATCH_POLL_S", str(_DEFAULT_POLL_S))))
    except (TypeError, ValueError):
        return _DEFAULT_POLL_S


def _cid(ticker, id2tk):
    """A batch custom_id must match ^[a-zA-Z0-9_-]{1,64}$ and be unique within the batch.
    Sanitise the ticker and disambiguate collisions; keep the cid->ticker reverse map."""
    base = re.sub(r"[^A-Za-z0-9_-]", "_", str(ticker))[:48] or "asset"
    cid, i = base, 1
    while cid in id2tk:
        i += 1
        cid = f"{base}_{i}"
    id2tk[cid] = ticker
    return cid


# The per-MTok model-family price table + the cost/usage maths now live in anthropic_client (ONE
# table, ONE formula). _model_prices / _telemetry below are thin delegates so the batch path shares
# the exact pricing the synchronous author/critic now use — the critic is costed at Haiku, not the
# old Sonnet over-estimate (~5x). Unknown model ids fall back to brief_writer's env Sonnet prices.


def _model_prices(model):
    """(price_in, price_out) per MTok for `model` — thin wrapper over anthropic_client.resolve_prices
    with brief_writer's env-configured Sonnet prices as the unrecognised-model fallback (and its
    explicit env override, when set, taking precedence over the model table)."""
    return resolve_prices(model, (bw.PRICE_IN_PER_MTOK, bw.PRICE_OUT_PER_MTOK), bw.PRICE_OVERRIDE)


def _telemetry(message, model):
    """Per-request token + web-search usage, costed at BATCH (50%) prices via the shared
    AnthropicBriefClient.usage()/cost(). Best-effort: the ledger records the live usage numbers; this
    drives the manifest's est_cost_usd only. input_tokens is the NON-cache input (same basis as the
    synchronous writer's telemetry); cache reads/writes are tracked separately, folded into cost."""
    abc = AnthropicBriefClient(None, model, batch=True,
                               price_fallback=(bw.PRICE_IN_PER_MTOK, bw.PRICE_OUT_PER_MTOK),
                               price_override=bw.PRICE_OVERRIDE)
    u = abc.usage(message)
    cost = abc.cost(u["input"], u["output"], u["cache_read"], u["cache_write"])
    return {"model": model, "input_tokens": u["input"], "output_tokens": u["output"],
            "web_searches": u["web_search"], "cache_read_tokens": u["cache_read"],
            "est_cost_usd": round(cost, 4), "batch": True}


def _merge_tele(a, b):
    """Sum the token/web/cost telemetry across the author round + its repair round."""
    a = a or {}
    b = b or {}
    return {"model": b.get("model") or a.get("model"),
            "input_tokens": (a.get("input_tokens", 0) or 0) + (b.get("input_tokens", 0) or 0),
            "output_tokens": (a.get("output_tokens", 0) or 0) + (b.get("output_tokens", 0) or 0),
            "web_searches": (a.get("web_searches", 0) or 0) + (b.get("web_searches", 0) or 0),
            "cache_read_tokens": (a.get("cache_read_tokens", 0) or 0) + (b.get("cache_read_tokens", 0) or 0),
            "est_cost_usd": round((a.get("est_cost_usd", 0.0) or 0.0) + (b.get("est_cost_usd", 0.0) or 0.0), 4),
            "batch": True}


def _err_str(result):
    """Best-effort human string for a non-succeeded batch result (errored/expired/canceled)."""
    rt = getattr(result, "type", "error")
    err = getattr(result, "error", None)
    if err is not None:
        inner = getattr(err, "error", err)
        msg = getattr(inner, "message", None) or getattr(err, "message", None)
        if msg:
            return f"{rt}: {msg}"[:200]
    return rt


def _run_batch(client, reqs, *, label, poll_interval, deadline):
    """Submit reqs = [(custom_id, params_dict)], poll until ended OR the absolute `deadline` (epoch
    seconds), return ({cid: message}, {cid: err}). Raises BatchTimeout / submission errors so the
    caller can fall back to the synchronous path. Per-request failures go in errs, never raised.

    `deadline` is a SHARED wall-clock budget (author + repair + critic all draw from it) so total
    batch polling can't overrun the systemd run timeout and get the whole run SIGTERM'd mid-poll."""
    if not reqs:
        return {}, {}
    # Bound EVERY batch HTTP call (create + retrieve + results) so a hung submission/poll can't block
    # indefinitely past the deadline — the un-wrapped client would submit at the SDK default (600s) x
    # max_retries=8, which is effectively unbounded.
    poll = client.with_options(timeout=120.0) if hasattr(client, "with_options") else client
    batch = poll.messages.batches.create(
        requests=[{"custom_id": cid, "params": params} for cid, params in reqs])
    bid = batch.id

    while True:
        info = poll.messages.batches.retrieve(bid)
        if getattr(info, "processing_status", None) == "ended":
            break
        remaining = deadline - time.time()
        if remaining <= 0:
            try:
                poll.messages.batches.cancel(bid)
            except Exception:
                pass
            raise BatchTimeout(f"{label} batch {bid} exceeded the shared batch budget")
        time.sleep(min(poll_interval, max(1.0, remaining)))

    msgs, errs = {}, {}
    for r in poll.messages.batches.results(bid):
        cid = getattr(r, "custom_id", None)
        if not cid:                        # a result with no custom_id can't be matched — skip, don't
            continue                       # let multiple None-cid rows collide on one key + drop assets
        res = getattr(r, "result", None)
        rt = getattr(res, "type", None)
        if rt == "succeeded":
            msgs[cid] = res.message
        else:
            errs[cid] = _err_str(res)
    return msgs, errs


# --------------------------------------------------------------------- authoring

def _author_params(ticker, analysis, memory_pack, research, social, *, model, max_tokens,
                   guidance, include_news):
    """Build the Messages params for one brief-author request — same shape brief_writer.author_brief
    sends synchronously (system + web_search tool + user message), with the rules block cached."""
    web_uses, sys_suffix = bw._news_settings(include_news)
    system = [{"type": "text", "text": bw.SYSTEM_PROMPT + sys_suffix,
               "cache_control": {"type": "ephemeral"}}]
    user = bw.build_user_message(ticker, analysis, memory_pack, research, social, guidance)
    return {"model": model, "max_tokens": max_tokens, "system": system,
            "tools": [{"type": _WEB_TOOL_TYPE, "name": "web_search", "max_uses": web_uses}],
            "messages": [{"role": "user", "content": user}]}


def _repair_guidance(errs):
    return ("Your previous brief failed schema validation with these errors:\n- "
            + "\n- ".join(errs)
            + "\n\nReturn the COMPLETE corrected brief as a single JSON object (no prose, no "
            "fences). Fix every error and keep all other fields. Do not author prices or pad "
            "confidence.")


def _author_round(client, items, *, model, max_tokens, guidance_map, poll_interval, deadline):
    """One author batch. Returns {ticker: {"brief", "telemetry", "error", "errs"}} where `errs` is
    the schema-validation error list (non-None => eligible for a repair round)."""
    reqs, id2tk = [], {}
    for it in items:
        tk = it["ticker"]
        cid = _cid(tk, id2tk)
        reqs.append((cid, _author_params(
            tk, it["analysis"], it["memory_pack"], it.get("research"), it.get("social"),
            model=model, max_tokens=max_tokens, guidance=guidance_map.get(tk),
            include_news=bool(it.get("include_news", True)))))
    msgs, errs = _run_batch(client, reqs, label="author", poll_interval=poll_interval, deadline=deadline)

    out = {}
    for cid, tk in id2tk.items():
        if cid not in msgs:
            out[tk] = {"brief": None, "telemetry": {}, "error": errs.get(cid, "no batch result"),
                       "errs": None}
            continue
        msg = msgs[cid]
        brief, perr = bw._extract_json(msg.content)
        verrs = [perr] if perr else bw.validate_brief(brief)
        tele = _telemetry(msg, model)
        if verrs:
            out[tk] = {"brief": None, "telemetry": tele, "error": "; ".join(verrs)[:240],
                       "errs": verrs}
        else:
            out[tk] = {"brief": brief, "telemetry": tele, "error": None, "errs": None}
    return out


def author_briefs(items, *, model, max_tokens, poll_interval=None, deadline=None):
    """Author every brief in ONE batch (+ one repair batch for schema-failers). Returns
    {ticker: {"brief": dict|None, "telemetry": {...}, "error": str|None}}. Raises on a submission/
    timeout failure so run_daily can fall back to the synchronous writer. `deadline` (absolute epoch
    seconds) is the SHARED budget for round 1 + the repair round."""
    poll_interval = poll_interval or _poll_s()
    if deadline is None:
        deadline = time.time() + _batch_timeout_s()
    client = bw._client(bw._require_sdk())

    r1 = _author_round(client, items, model=model, max_tokens=max_tokens, guidance_map={},
                       poll_interval=poll_interval, deadline=deadline)

    # Mirror the synchronous writer's single re-prompt: re-author the schema-failers ONCE with their
    # validation errors as guidance. (A batch request can't self-correct mid-flight.) Shares the same
    # deadline as round 1 — the repair can't extend total batch time past the budget.
    repair = [it for it in items if r1.get(it["ticker"], {}).get("errs")]
    if repair:
        gmap = {it["ticker"]: _repair_guidance(r1[it["ticker"]]["errs"]) for it in repair}
        r2 = _author_round(client, repair, model=model, max_tokens=max_tokens, guidance_map=gmap,
                           poll_interval=poll_interval, deadline=deadline)
        for tk, r in r2.items():
            r["telemetry"] = _merge_tele(r1[tk]["telemetry"], r.get("telemetry"))
            r1[tk] = r

    return {tk: {"brief": r["brief"], "telemetry": r.get("telemetry", {}), "error": r["error"]}
            for tk, r in r1.items()}


# --------------------------------------------------------------------- critique

# Re-prompt directive for the critic repair round — mirrors critic.review_brief's attempt-2 text.
_CRITIC_REPAIR_DIRECTIVE = ("\n\nReturn ONLY a single, COMPLETE, compact JSON verdict object — no "
                            "prose, no markdown fences. Keep the 'issues' list concise (the most "
                            "important items) so the JSON closes.")


def _critic_round(client, items, *, model, max_tokens, extra_directive, poll_interval, deadline):
    """One critic batch. Returns {ticker: {"verdict": dict|None, "failed": bool}} where failed=True
    means the verdict was missing / unparseable / malformed (eligible for a repair round)."""
    reqs, id2tk = [], {}
    for it in items:
        tk = it["ticker"]
        cid = _cid(tk, id2tk)
        system = [{"type": "text", "text": cr.SYSTEM_PROMPT,
                   "cache_control": {"type": "ephemeral"}}]
        user = cr.build_user_message(tk, it["brief"], it.get("analysis"), it.get("research"))
        if extra_directive:
            user += extra_directive
        reqs.append((cid, {"model": model, "max_tokens": max_tokens, "system": system,
                           "messages": [{"role": "user", "content": user}]}))
    msgs, _errs = _run_batch(client, reqs, label="critic", poll_interval=poll_interval, deadline=deadline)

    out = {}
    for cid, tk in id2tk.items():
        if cid not in msgs:
            out[tk] = {"verdict": None, "failed": True}
            continue
        verdict, perr = cr._extract_json(msgs[cid].content)
        if perr or cr._verdict_errors(verdict):
            out[tk] = {"verdict": None, "failed": True}
            continue
        # Same defensive coherence guard as critic.review_brief: an approve must carry no blockers.
        if verdict["decision"] == "approve" and verdict.get("publish_blockers"):
            verdict["decision"] = "revise"
            verdict.setdefault("summary", "")
            verdict["summary"] += " [downgraded approve->revise: publish_blockers were present]"
        verdict["_telemetry"] = _telemetry(msgs[cid], model)
        out[tk] = {"verdict": verdict, "failed": False}
    return out


def review_briefs(items, *, model, max_tokens, poll_interval=None, deadline=None):
    """Critique every brief in ONE batch (+ ONE repair batch for truncated/malformed verdicts).
    Returns {ticker: verdict_dict|None} (None == still unusable after the repair — the asset then
    degrades to needs_brief). The repair round is the CRITICAL parity with the synchronous critic's
    attempt-2 retry: a verbose adversarial verdict (common on technical crypto/FX briefs) can hit the
    output ceiling and truncate the JSON; without the retry that silently drops the brief. Raises on
    submission/timeout."""
    poll_interval = poll_interval or _poll_s()
    if deadline is None:
        deadline = time.time() + _batch_timeout_s()
    # Guarantee the (cheap, fast Haiku) critic a minimum slice even if authoring consumed the shared
    # budget — the briefs are already authored + paid for; don't waste them on a clock expiry.
    deadline = max(deadline, time.time() + 300)
    client = bw._client(bw._require_sdk())

    by_tk = {it["ticker"]: it for it in items}
    r1 = _critic_round(client, items, model=model, max_tokens=max_tokens, extra_directive="",
                       poll_interval=poll_interval, deadline=deadline)

    # Repair: re-critique the failed verdicts ONCE, demanding a compact COMPLETE object (with a >=8000
    # token floor). The compact ask is what recovers a verbose verdict that truncated.
    repair = [by_tk[tk] for tk, r in r1.items() if r["failed"]]
    if repair:
        r2 = _critic_round(client, repair, model=model, max_tokens=max(max_tokens, 8000),
                           extra_directive=_CRITIC_REPAIR_DIRECTIVE, poll_interval=poll_interval,
                           deadline=deadline)
        r1.update(r2)

    return {tk: r["verdict"] for tk, r in r1.items()}
