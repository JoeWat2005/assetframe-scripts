"""anthropic_client.py — the single Anthropic client/usage/cost surface for the brief pipeline.

The Messages-API call, the token-usage extraction and the cost/pricing maths used to be
copy-pasted across brief_writer.py (sync author), critic.py (sync reviewer) and brief_batch.py
(batched author/critic) with THREE divergent pricing paths:
  * the sync author priced every call at a Sonnet-fixed rate (so a `--model` opus/haiku run was
    mispriced),
  * the sync critic did not price its call at all, and
  * only the batch path was model + cache + batch-discount aware.
brief_batch documented that the old shared path costed the Haiku critic at Sonnet prices — a ~5x
overstatement. This module collapses the triad into ONE place:

  AnthropicBriefClient(client, model, ...)
    .usage(resp)                  -> one token-usage extraction (input/output/web/cache)
    .cost(input, output, ...)     -> ONE pricing formula (cache 0.1x read / 1.25x write, batch 0.5x)
    .author(...)                  -> the brief-author flow (web_search + pause_turn resume + repair)
    .critique(...)                -> the adversarial-review flow (no tools + max_tokens bump retry)

DEPENDENCY INJECTION (load-bearing): the class NEVER builds its own SDK client. brief_writer and
critic each keep their module-level `_client` / `_require_sdk` factories (the seam the tests patch),
resolve the SDK through them, and INJECT the handle here. `author()`/`critique()` reach the
module-specific prompt/parse helpers (SYSTEM_PROMPT, build_user_message, _extract_json, validators)
via a function-local import of the calling module, which also breaks the import cycle
(brief_writer/critic import this module at load time; this module imports them only when a method
runs, by which point they are fully loaded).
"""
import sys

# Standard per-MTok (input, output) USD list prices by model family. Halved for batch in cost().
# Moved here from brief_batch so all three call sites resolve a model's price from ONE table.
_MODEL_PRICES = {"haiku": (1.0, 5.0), "sonnet": (3.0, 15.0), "opus": (15.0, 75.0)}


def resolve_prices(model, fallback=None):
    """(price_in, price_out) per MTok for a model id, by family substring. Unknown models fall back
    to `fallback` (brief_writer's env-configurable Sonnet prices at the call sites) or list Sonnet."""
    m = (model or "").lower()
    for key, price in _MODEL_PRICES.items():
        if key in m:
            return price
    return fallback if fallback is not None else _MODEL_PRICES["sonnet"]


class AnthropicBriefClient:
    """Holds an INJECTED Anthropic SDK client + the model and its resolved prices, and owns the
    one-true call/usage/cost helpers plus the author/critique flows. Construct it from the calling
    module's `_client(_require_sdk())` so the DI seam (and the tests' monkeypatches) keep working —
    never let it build a client itself."""

    def __init__(self, client, model, *, batch=False, default_max_tokens=None, price_fallback=None):
        self.client = client
        self.model = model
        self.batch = batch
        self.default_max_tokens = default_max_tokens
        # Resolve the price ONCE (model-aware) — this is the fix for the critic's Sonnet-priced cost.
        self.price_in, self.price_out = resolve_prices(model, price_fallback)

    # ------------------------------------------------------------------ primitives

    def _create(self, **kw):
        """client.messages.create(**kw) with the identical API-error handling the sync author and
        critic each carried — a network / auth / rate-limit failure prints a clear line and exits 3
        (run_daily catches the non-zero exit and degrades the asset to needs_brief)."""
        try:
            return self.client.messages.create(**kw)
        except Exception as ex:           # network / auth / rate limit
            print(f"ERROR: Anthropic API call failed: {type(ex).__name__}: {ex}", file=sys.stderr)
            sys.exit(3)

    def usage(self, resp):
        """One token-usage extraction. Returns a dict of input / output / web_search / cache_read /
        cache_write counts (replaces brief_writer._acc, brief_batch._telemetry's extraction and the
        critic's inline read). Missing fields default to 0 so partial test/stub usages are safe."""
        u = getattr(resp, "usage", None)

        def g(name):
            return getattr(u, name, 0) or 0

        srv = getattr(u, "server_tool_use", None)
        return {
            "input": g("input_tokens"),
            "output": g("output_tokens"),
            "web_search": (getattr(srv, "web_search_requests", 0) or 0) if srv else 0,
            "cache_read": g("cache_read_input_tokens"),
            "cache_write": g("cache_creation_input_tokens"),
        }

    def cost(self, input, output, cache_read=0, cache_write=0):
        """The ONE cost formula (USD). Cache reads bill at 0.1x, cache writes at 1.25x; a batch call
        gets the 0.5x discount. Best-effort estimate only — the ledger records the live usage."""
        billable_in = input + 0.1 * cache_read + 1.25 * cache_write
        c = (billable_in / 1e6) * self.price_in + (output / 1e6) * self.price_out
        return 0.5 * c if self.batch else c

    # ------------------------------------------------------------------ author flow

    def author(self, ticker, analysis, memory_pack, research, social, *, guidance=None,
               include_news=True, max_tokens=None):
        """Author a brief: build the web_search-enabled request, drive the server tool's pause_turn
        resume loop, parse + schema-validate, and RE-PROMPT ONCE on a validation miss. Returns
        (brief, telemetry); raises SystemExit on a hard failure. (Body of brief_writer.author_brief.)"""
        import brief_writer as bw          # local import: breaks the load-time cycle + keeps the seam
        max_tokens = max_tokens or self.default_max_tokens

        web_uses, sys_suffix = bw._news_settings(include_news)
        system = [{"type": "text", "text": bw.SYSTEM_PROMPT + sys_suffix,
                   "cache_control": {"type": "ephemeral"}}]
        user = bw.build_user_message(ticker, analysis, memory_pack, research, social, guidance)
        messages = [{"role": "user", "content": user}]
        tools = [{"type": "web_search_20250305", "name": "web_search", "max_uses": web_uses}]

        tot_in = tot_out = tot_web = 0
        last_err = None

        def _acc(r):                       # accumulate token + web-search usage across calls
            nonlocal tot_in, tot_out, tot_web
            u = self.usage(r)
            tot_in += u["input"]
            tot_out += u["output"]
            tot_web += u["web_search"]

        def _call():
            return self._create(model=self.model, max_tokens=max_tokens, system=system,
                                tools=tools, messages=messages)

        # attempt 1 = author; attempt 2 = repair with the validation errors fed back.
        for attempt in (1, 2):
            resp = _call()
            _acc(resp)
            # web_search can PAUSE the turn (stop_reason=='pause_turn') with NO finished JSON; RESUME
            # the same turn by re-sending the conversation with the model's partial content appended
            # (not a validation repair). Bounded so a loop can't run away.
            resumes = 0
            while getattr(resp, "stop_reason", None) == "pause_turn" and resumes < 5:
                resumes += 1
                messages.append({"role": "assistant", "content": resp.content})
                resp = _call()
                _acc(resp)

            brief, perr = bw._extract_json(resp.content)
            errs = [perr] if perr else bw.validate_brief(brief)
            if errs and getattr(resp, "stop_reason", None) == "max_tokens":
                # Output hit the token ceiling, so the JSON was cut off. Name the real cause.
                errs = [f"model hit max_tokens={max_tokens} (output truncated before the JSON "
                        f"closed); raise --max-tokens"] + errs
            telemetry = {"model": self.model, "input_tokens": tot_in, "output_tokens": tot_out,
                         "web_searches": tot_web, "attempts": attempt,
                         "est_cost_usd": round(self.cost(tot_in, tot_out), 4)}
            if not errs:
                return brief, telemetry

            last_err = errs
            if attempt == 1:
                # Re-prompt once: keep the model's own draft in the thread and ask for a corrected
                # COMPLETE object addressing the listed errors.
                messages.append({"role": "assistant", "content": resp.content})
                messages.append({"role": "user", "content":
                                 "Your brief failed schema validation with these errors:\n- "
                                 + "\n- ".join(errs)
                                 + "\n\nReturn the COMPLETE corrected brief as a single JSON "
                                 "object (no prose, no fences). Fix every error and keep all "
                                 "other fields. Do not author prices or pad confidence."})

        # still invalid after the repair attempt
        print("ERROR: brief failed schema validation after re-prompt:\n  - "
              + "\n  - ".join(last_err or ["unknown error"]), file=sys.stderr)
        print(bw._usage_line(self.model, tot_in, tot_out, tot_web, 2), file=sys.stderr)
        sys.exit(2)

    # ------------------------------------------------------------------ critique flow

    def critique(self, asset, brief, analysis, research, *, max_tokens=None):
        """Adversarially review a brief: one no-tools call, parse + validate the verdict, and retry
        ONCE (bumping max_tokens on a confirmed truncation) for a single compact COMPLETE object.
        Returns (verdict, telemetry); raises SystemExit(3) on API error or an unrecoverable verdict.
        (Body of critic.review_brief.)"""
        import critic as cr                # local import: breaks the load-time cycle + keeps the seam
        max_tokens = max_tokens or self.default_max_tokens

        system = [{"type": "text", "text": cr.SYSTEM_PROMPT,   # cached rubric (static across assets)
                   "cache_control": {"type": "ephemeral"}}]
        base_user = cr.build_user_message(asset, brief, analysis, research)
        messages = [{"role": "user", "content": base_user}]
        in_tok = out_tok = 0
        verdict = None
        last_errs = ["unknown error"]

        for attempt in (1, 2):
            resp = self._create(model=self.model, max_tokens=max_tokens, system=system,
                                messages=messages)
            u = self.usage(resp)
            in_tok += u["input"]
            out_tok += u["output"]

            verdict, perr = cr._extract_json(resp.content)
            errs = [perr] if perr else cr._verdict_errors(verdict)
            if not errs:
                break
            last_errs = errs
            if attempt == 1:
                # If the output hit the ceiling, the JSON was cut off — bump the budget. Always ask
                # for a single compact, complete object so the verdict closes.
                if getattr(resp, "stop_reason", None) == "max_tokens":
                    max_tokens = max(max_tokens, 8000)
                messages = [{"role": "user", "content": base_user + "\n\nReturn ONLY a single, "
                             "COMPLETE, compact JSON verdict object — no prose, no markdown fences. "
                             "Keep the 'issues' list concise (the most important items) so the JSON "
                             "closes."}]
                continue
            print("ERROR: critic produced an invalid verdict:\n  - " + "\n  - ".join(last_errs),
                  file=sys.stderr)
            sys.exit(3)

        # defensive coherence: an approve must not carry publish blockers
        if verdict["decision"] == "approve" and verdict.get("publish_blockers"):
            verdict["decision"] = "revise"
            verdict.setdefault("summary", "")
            verdict["summary"] += " [downgraded approve->revise: publish_blockers were present]"

        telemetry = {"model": self.model, "input_tokens": in_tok, "output_tokens": out_tok,
                     "est_cost_usd": round(self.cost(in_tok, out_tok), 4)}
        return verdict, telemetry
