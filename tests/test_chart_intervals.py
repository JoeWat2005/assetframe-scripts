"""Tests for selectable analysis intervals (chart_intervals):
  - intraday: validation (skip-invalid, never crash), resampling, series build
  - config_loader: validation + normalization (default + force-include canonical)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import intraday as I
import config_loader as CL


def _rows(seq):
    """seq = list of (ts, o, h, l, c, v) -> intraday row dicts."""
    return [{"ts": ts, "o": o, "h": h, "l": l, "c": c, "v": v} for ts, o, h, l, c, v in seq]


def test_parse_chart_intervals_drops_invalid_and_forces_canonical():
    # bogus values are skipped (never raise); canonical 60m+1d always present
    assert I.parse_chart_intervals("4h,bogus,1week") == ["60m", "1d", "4h", "1week"]
    # empty -> the canonical pair
    assert I.parse_chart_intervals("") == ["60m", "1d"]
    # only-invalid -> still the canonical pair (engine never left with nothing)
    assert I.parse_chart_intervals("nope,zzz") == ["60m", "1d"]
    # dedupe + order preserved (canonical first)
    assert I.parse_chart_intervals("1d,60m,4h,4h") == ["60m", "1d", "4h"]


def test_resample_hours_aggregates_ohlcv():
    # four 1h bars 00:00..03:00 -> one 4h bucket
    base = 1_700_000_000 - (1_700_000_000 % (4 * 3600))  # 4h-aligned
    rows = _rows([(base + i * 3600, 10 + i, 12 + i, 9 + i, 11 + i, 100) for i in range(4)])
    out = I._resample_hours(rows, 4)
    assert len(out) == 1
    b = out[0]
    assert b["o"] == rows[0]["o"]          # first open
    assert b["c"] == rows[-1]["c"]         # last close
    assert b["h"] == max(r["h"] for r in rows)
    assert b["l"] == min(r["l"] for r in rows)
    assert b["v"] == sum(r["v"] for r in rows)


def test_resample_calendar_weekly_monthly():
    day = 86400
    # 7 consecutive daily bars starting Mon 2023-01-02 (ISO week 1) span one ISO week
    start = 1_672_617_600  # 2023-01-02 00:00 UTC (Monday)
    daily = _rows([(start + i * day, 5.0, 6.0 + i, 4.0, 5.5 + i, 10) for i in range(7)])
    wk = I._resample_calendar(daily, "1week")
    assert len(wk) == 1
    assert wk[0]["c"] == daily[-1]["c"]
    assert wk[0]["h"] == max(r["h"] for r in daily)
    mo = I._resample_calendar(daily, "1month")
    assert len(mo) == 1                    # all 7 days are in January


def test_build_interval_series_routes_correctly():
    hourly = _rows([(i * 3600, 1, 2, 0.5, 1.5, 1) for i in range(8)])
    daily = _rows([(i * 86400, 1, 2, 0.5, 1.5, 1) for i in range(40)])
    assert I.build_interval_series("60m", hourly, daily) == hourly
    assert I.build_interval_series("1d", hourly, daily) == daily
    assert len(I.build_interval_series("4h", hourly, daily)) == 2   # 8 hourly / 4
    assert I.build_interval_series("zzz", hourly, daily) == []      # unknown -> empty, no crash


def test_interval_block_is_metadata_safe_on_empty():
    blk = I.interval_block("4h", [])
    assert blk["bars"] == 0 and blk["last_close"] is None
    assert blk["trend"] == "Insufficient data"


def test_twelvedata_unsupported_interval_raises():
    # the silent ".get(...,'1day')" default is gone: an unknown interval is explicit
    try:
        I.twelvedata_chart("AAPL", "13x", "1y", "k", td_symbol="AAPL")
    except ValueError as ex:
        assert "interval" in str(ex)
    else:
        raise AssertionError("expected ValueError for unsupported interval")


def test_config_loader_validates_and_normalizes_chart_intervals():
    base = {"id": "x", "name": "X", "instrument": "X", "ticker": "X",
            "provider_symbols": {"yahoo": "X"}, "asset_class": "crypto",
            "session_profile": "crypto_24_7", "cadence": "daily", "timezone": "UTC"}
    # bad interval is reported
    errs = CL._validate_one({**base, "chart_intervals": ["4h", "bogus"]}, "asset[0]", set())
    assert any("bogus" in e for e in errs)
    # empty list rejected
    assert any("non-empty" in e for e in CL._validate_one({**base, "chart_intervals": []}, "a", set()))
    # valid passes
    assert not any("chart_interval" in e for e in
                   CL._validate_one({**base, "chart_intervals": ["60m", "4h", "1d"]}, "a", set()))
    # normalize: default is canonical pair; force-include canonical even if omitted
    assert CL._normalize(base)["chart_intervals"] == ["60m", "1d"]
    assert CL._normalize({**base, "chart_intervals": ["4h"]})["chart_intervals"] == ["60m", "1d", "4h"]


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok {name}")
    print("all chart_intervals tests passed")
