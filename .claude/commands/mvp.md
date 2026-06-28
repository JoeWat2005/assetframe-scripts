---
description: Generate the AssetFrame Snapshot + Pro report pair for an instrument
argument-hint: "<ticker, futures symbol, FX pair, or crypto asset>"
---

Load the `mvp` skill and run the AssetFrame report pipeline on: $ARGUMENTS

Follow the skill exactly: score any expired ledger windows first, run the engine and gather context (never fabricate), apply the instrument's session profile, build the canonical payload, generate the Snapshot + Pro pair with `scripts/pipeline/render/mvp_report.py` (the QA gate must pass), register the Pro predictions for the report's window, then visually inspect both PDFs page by page and stamp with `--stamp-visual`. Return the output paths, QA checklist, and remaining data gaps.
