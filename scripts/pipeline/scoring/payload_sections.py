"""payload_sections.py — leaf HTML section builders for the Pro report.

Pure string assemblers: each takes compiled data and returns an HTML fragment. No imports
(extracted from scaffold_payload; scaffold re-exports them).
"""


def _scenario_matrix_html(rows):
    h = ("<table><tr><th>Case</th><th>Trigger</th><th>Expected move/range</th>"
         "<th>Invalidation</th><th>Confidence</th><th>What to watch</th></tr>")
    for r in rows:
        h += (f"<tr><td>{r.get('case','')}</td><td>{r.get('trigger','')}</td><td>{r.get('move','')}</td>"
              f"<td>{r.get('invalidation','')}</td><td>{r.get('confidence','')}</td><td>{r.get('watch','')}</td></tr>")
    return h + "</table>"


def _events_html(cats):
    h = ("<table><tr><th>Event</th><th>Time</th><th>Relevance</th><th>In window?</th><th>Gap risk?</th></tr>")
    for c in cats:
        h += (f"<tr><td>{c.get('label','')}</td><td>{c.get('when','')}</td><td>{c.get('relevance','')}</td>"
              f"<td>{'Yes' if c.get('in_window') else 'No'}</td><td>{'Yes' if c.get('gap_risk') else '-'}</td></tr>")
    return h + "</table>"


def _technicals_html(analysis, levels, last_price, nb):
    pre = nb.get("technicals_note", "")
    h = (f"<p>{pre}</p>" if pre else "") + ("<table><tr><th>Level</th><th>Price</th>"
         "<th>Distance</th><th>Classification</th></tr>")
    for l in levels:
        dist = l["value"] - last_price
        h += (f"<tr><td>{l['label']}</td><td>{l['value']}</td><td>{dist:+.2f}</td>"
              f"<td>{l['cls'].title()}</td></tr>")
    return h + "</table>"


def _setups_html(setups):
    h = ("<table><tr><th>Setup</th><th>Dir</th><th>Entry zone</th><th>Invalidation</th>"
         "<th>T1 / T2</th><th>R:R</th></tr>")
    for s in setups:
        h += (f"<tr><td>{s['name']}</td><td>{s['direction'].title()}</td>"
              f"<td>{s['entry_lo']} - {s['entry_hi']}</td><td>{s['invalidation']}</td>"
              f"<td>{s.get('t1')} / {s.get('t2')}</td><td>{s['rr']}</td></tr>")
    return h + "</table>"


def _scorecard_html(conf):
    h = "<table><tr><th>Component</th><th>Weight</th><th>Score</th></tr>"
    for c in conf["components"]:
        sc = f"{c['score']:.2f}" if isinstance(c["score"], float) else c["score"]
        wt = f"{c['weight']}%" if c["weight"] else "adj"
        h += f"<tr><td>{c['name']}</td><td>{wt}</td><td>{sc}</td></tr>"
    h += "</table><ul>"
    h += f"<li><b>Published confidence: {conf['published']}/100</b> ({conf['band']}); raw {conf['raw']}.</li>"
    if conf["caps_applied"]:
        h += f"<li>Caps applied: {', '.join(conf['caps_applied'])}.</li>"
    h += (f"<li>Calibration: {'applied from the ledger' if conf['calibrated'] else 'identity (too few scored rows yet)'}"
          f"; engine v{conf['conf_version']}. The analyst explains this score; the engine computes it.</li></ul>")
    return h


def _ledger_html(brief, ledger_levels):
    return ("<ul><li><b>Ledger:</b> this report's window is registered; predictions are scored "
            "against the tape after the window closes (Hit / Miss / No trigger / Manual review).</li></ul>"
            "<p>Levels under test: " + ", ".join(str(x) for x in ledger_levels) + ".</p>")


def _source_audit_html(brief, analysis, dq):
    prov = analysis.get("provider") or {}
    hp = prov.get("hourly") or "engine"
    dp = prov.get("daily")
    src = hp if (not dp or dp == hp) else f"{hp} (hourly) + {dp} (daily)"   # G6: don't hide a split source
    gaps = brief.get("source_gaps") or (brief.get("news_context") or {}).get("source_gaps", [])
    h = (f"<ul><li><b>Primary data provider:</b> project intraday engine ({src}).</li>"
         f"<li><b>Cross-check:</b> {brief.get('cross_check','single-source this run')}.</li>")
    if prov.get("license_mode") == "commercial":
        if prov.get("license_degraded"):
            h += ("<li><b>&#9888; Data licensing:</b> this edition fell back to a "
                  "non-commercially-licensed source — not for redistribution.</li>")
        else:
            h += "<li><b>Data licensing:</b> commercially-licensed feed.</li>"
    if gaps:
        h += "<li><b>Gaps:</b> " + "; ".join(gaps) + ".</li>"
    h += f"<li><b>Overall data quality: {dq}/10.</b></li></ul>"
    return h
