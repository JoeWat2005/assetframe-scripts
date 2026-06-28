#!/usr/bin/env python3
"""Firewall test (Task T17) — the distribution feedback loop is MARKETING-ONLY and must
stay walled off from research scoring.

It asserts that NONE of the research/confidence/ledger scoring modules read any marketing
metric, and that the web engagement recorder never imports the scoring path. If clean it
prints "FIREWALL OK" and exits 0; otherwise it lists every violation and exits 1.

Why this matters: engagement (impressions / clicks / likes) is a popularity signal. If it
ever fed back into confidence, bias, or outcome scoring, the system would start optimising
for what spreads instead of what's correct. This test makes that regression a build failure.

Run:  python -m pytest tests/test_firewall.py
(stdlib only; resolves its own paths, so cwd doesn't actually matter.)
"""
import re
import sys
from pathlib import Path

# mvp/ root, resolved from this file's location (tests/test_firewall.py).
ROOT = Path(__file__).resolve().parent.parent

# The research-scoring modules that MUST NOT know marketing metrics exist.
SCORING_MODULES = [
    "scripts/pipeline/scoring/confidence.py",
    "scripts/analytics/store/calibrate.py",
    "scripts/analytics/memory/ledger_context.py",
    "scripts/analytics/memory/research_memory.py",
    "scripts/pipeline/scoring/score_report.py",
    "scripts/pipeline/scoring/scaffold_payload.py",
]

# Marketing-only terms. A scoring module referencing any of these breaches the firewall.
# Matched as whole words (case-insensitive) so unrelated substrings don't false-positive.
BANNED_TERMS = [
    "social_engagement",
    "engagement",
    "impressions",
    "clicks",
    "report_views",
    "download_log",
]

# The web recorder, which may touch marketing data but MUST NOT import the scoring path.
ENGAGEMENT_LIB = "web/lib/engagement.ts"
# Module/file stems on the research/confidence/ledger side. The recorder importing any of
# these would couple marketing to scoring — the reverse direction of the same firewall.
SCORING_IMPORT_TOKENS = [
    "confidence",
    "calibrate",
    "ledger_context",
    "ledger",
    "research_memory",
    "score_report",
    "scaffold_payload",
    "taxonomy",
]


def _read(rel):
    """Return (path, text). Missing file is itself a violation (caller records it)."""
    p = ROOT / rel
    if not p.is_file():
        return p, None
    return p, p.read_text(encoding="utf-8", errors="replace")


def main():
    checked = []
    violations = []

    # 1) No scoring module may reference a marketing metric.
    banned_re = {
        t: re.compile(r"\b" + re.escape(t) + r"\b", re.IGNORECASE) for t in BANNED_TERMS
    }
    for rel in SCORING_MODULES:
        p, text = _read(rel)
        checked.append(rel)
        if text is None:
            violations.append("MISSING (cannot verify firewall): " + rel)
            continue
        for term, rx in banned_re.items():
            for i, line in enumerate(text.splitlines(), 1):
                if rx.search(line):
                    violations.append(
                        "{}:{} references marketing metric '{}': {}".format(
                            rel, i, term, line.strip()
                        )
                    )

    # 2) The engagement recorder must not import the scoring/ledger path.
    p, text = _read(ENGAGEMENT_LIB)
    if text is None:
        # Split engine repo: the web recorder lives in assetframe-infra, which verifies
        # engagement.ts's imports itself. Absent here is a SKIP, not a firewall violation.
        checked.append(ENGAGEMENT_LIB + " (absent — infra repo, skipped)")
    else:
        checked.append(ENGAGEMENT_LIB)
        # Only inspect import/require statements, so prose in comments can mention these
        # words (this file's own header explains the firewall) without tripping the test.
        import_lines = [
            ln
            for ln in text.splitlines()
            if re.match(r"\s*import\b", ln) or "require(" in ln
        ]
        for ln in import_lines:
            for tok in SCORING_IMPORT_TOKENS:
                if re.search(r"\b" + re.escape(tok) + r"\b", ln):
                    violations.append(
                        "{} imports from the scoring path ('{}'): {}".format(
                            ENGAGEMENT_LIB, tok, ln.strip()
                        )
                    )

    # Report.
    print("Firewall check — research scoring must not read marketing metrics.")
    print("Root: {}".format(ROOT))
    print("Banned terms: {}".format(", ".join(BANNED_TERMS)))
    print("Checked {} files:".format(len(checked)))
    for rel in checked:
        print("  - {}".format(rel))

    if violations:
        print("")
        print("FIREWALL VIOLATION ({} issue(s)):".format(len(violations)))
        for v in violations:
            print("  ! {}".format(v))
        return 1

    print("")
    print("FIREWALL OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
