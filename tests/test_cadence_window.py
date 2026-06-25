"""Tests for per-period scoring windows + report_id period stamps:
  - sessions.get_cadence_window: daily/weekly/monthly window ends, ordering, scored_cadence tag
  - scaffold_payload._period_stamp: one id stamp per cadence period (year + ticker still parse)
"""
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sessions as S
import scaffold_payload as SP

UTC = timezone.utc
NOW = datetime(2026, 6, 24, 6, 0, tzinfo=UTC)   # Wednesday, mid-month


def _end(profile, cadence):
    w = S.get_cadence_window(profile, cadence, now=NOW)
    return datetime.strptime(w["window_end_utc"], "%Y-%m-%d %H:%M").replace(tzinfo=UTC), w


def test_cadence_window_ordering_and_tags():
    for profile in ("fx_spot", "cme_futures", "us_equity_rth", "crypto_24_7"):
        d_end, d = _end(profile, "daily")
        w_end, w = _end(profile, "weekly")
        m_end, m = _end(profile, "monthly")
        assert d["scored_cadence"] == "daily"
        assert w["scored_cadence"] == "weekly"
        assert m["scored_cadence"] == "monthly"
        # daily is the shortest window; all period closes are strictly after `now`.
        # (weekly vs monthly can cross when a rolling week spans the month boundary — expected.)
        assert NOW < d_end <= w_end, f"{profile}: daily {d_end} weekly {w_end}"
        assert NOW < d_end <= m_end, f"{profile}: daily {d_end} monthly {m_end}"


def test_unknown_cadence_falls_back_to_daily():
    w = S.get_cadence_window("fx_spot", "weekday", now=NOW)
    assert w["scored_cadence"] == "daily"


def test_monthly_window_lands_in_june():
    _, w = _end("fx_spot", "monthly")
    assert w["window_end_utc"].startswith("2026-06-"), w["window_end_utc"]


def test_period_stamp_daily_weekly_monthly():
    ws = "2026-06-24 06:00"
    assert SP._period_stamp("daily", ws, None) == "20260624"
    assert SP._period_stamp("weekly", ws, None) == "2026W26"     # ISO week 26 of 2026
    assert SP._period_stamp("monthly", ws, None) == "202606"
    # daily backdated keeps the per-minute stamp (seeds the track record fast)
    asof = datetime(2026, 6, 10, 14, 30, tzinfo=UTC)
    assert SP._period_stamp("daily", ws, asof) == "202606101430"
    # weekly/monthly ignore the backdate minute -> still one row per period
    assert SP._period_stamp("weekly", ws, asof) == "2026W26"


def test_report_id_forms_stay_parseable():
    # year = leading 4 digits; ticker = last '-' segment — true for every cadence form
    for stamp in ("20260624", "2026W26", "202606", "202606101430"):
        rid = f"AF-{stamp}-GBPUSD"
        assert rid.split("-")[1][:4] == "2026"
        assert rid.rsplit("-", 1)[-1] == "GBPUSD"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok {name}")
    print("all cadence_window tests passed")
