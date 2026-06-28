"""Shared ledger reading for the analytics memory layer (ledger_context + research_memory).

The append-only outcome ledger is read with the SAME hard no-look-ahead rule everywhere: only rows
whose prediction window closed STRICTLY before `as_of`. Centralised here so that rule + the row
parsing live in ONE place — they were previously duplicated byte-for-byte across the two modules.
"""
import csv
from datetime import datetime, timezone
from pathlib import Path


def parse_dt(s):
    try:
        return datetime.strptime(s.strip()[:16], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
    except (ValueError, AttributeError):
        return None


def ticker_of(report_id):
    return (report_id or "").rsplit("-", 1)[-1].strip().upper()


def rate(hits, misses):
    tot = hits + misses
    return round(100 * hits / tot, 1) if tot else None


def load_rows(ledger_path, as_of):
    """All scored rows whose window_end is strictly before `as_of` (no look-ahead), oldest-first.
    Each returned row gains _hits/_misses (int), _wend (datetime) and _ticker (from report_id).
    research_memory ignores _ticker (it aggregates across instruments); the extra key is harmless."""
    rows = []
    if not Path(ledger_path).exists():
        return rows
    with open(ledger_path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            wend = parse_dt(r.get("window_end_utc", ""))
            if wend is None or wend >= as_of:
                continue
            try:
                r["_hits"] = int(r.get("hits") or 0)
                r["_misses"] = int(r.get("misses") or 0)
            except ValueError:
                continue
            r["_ticker"] = ticker_of(r.get("report_id"))
            r["_wend"] = wend
            rows.append(r)
    rows.sort(key=lambda x: x["_wend"])
    return rows
