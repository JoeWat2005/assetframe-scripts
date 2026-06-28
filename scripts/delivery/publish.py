"""Upload report files to a private Cloudflare R2 bucket.

ALL report files — free Snapshots AND Pro reports — live in private R2 and are served
only through the auth-gated /api/report route in the Next.js app (free needs an account,
Pro needs a subscription). Nothing is public/static, so there is no way to read a report
without going through the gate.

R2 is S3-compatible, so this uses boto3. Install once:  pip install boto3

Set these environment variables (from the Cloudflare dashboard - see LAUNCH.md):
  R2_ACCOUNT_ID         your Cloudflare account id
  R2_ACCESS_KEY_ID      R2 API token access key
  R2_SECRET_ACCESS_KEY  R2 API token secret
  R2_BUCKET             the private bucket name (e.g. assetframe-pro)

Usage:
  python -m scripts.delivery.publish            upload every edition's free + Pro files
  python -m scripts.delivery.publish --dry-run  show what would upload, change nothing
  python -m scripts.delivery.publish --date 2026-06-13   only that edition date

Object keys mirror the paths /api/report requests: <date>/<slug>/{free,pro}.{html,pdf}
and <date>/<slug>/preview.png
"""
import argparse
import os
import sys
from pathlib import Path

from _paths import ROOT          # repo-root anchor (scripts/__init__ shim is on sys.path under -m)
from _r2 import R2Store          # shared env-load + boto3 client (deduped with r2_purge.py)
REPORTS = ROOT / "reports"
# Every report file is private in R2 now (free Snapshots AND Pro reports); the web app
# serves them only through the auth-gated /api/report route. Keys mirror the request path.
UPLOAD_FILES = {
    "free.html": "text/html; charset=utf-8",
    "free.pdf": "application/pdf",
    "preview.png": "image/png",
    "pro.html": "text/html; charset=utf-8",
    "pro.pdf": "application/pdf",
}


def discover(date_filter):
    items = []
    for meta in sorted(REPORTS.glob("*/*/metadata.json")):
        date, slug = meta.parent.parent.name, meta.parent.name
        if date.startswith("_"):
            continue
        if date_filter and date != date_filter:
            continue
        for name, ctype in UPLOAD_FILES.items():
            f = meta.parent / name
            if f.exists():
                items.append((f, f"{date}/{slug}/{name}", ctype))
    return items


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--date", default=None, help="only upload this edition date (YYYY-MM-DD)")
    a = ap.parse_args()

    items = discover(a.date)
    if not items:
        print("No report files found under reports/. Generate an edition first.")
        return

    if a.dry_run:
        print(f"DRY RUN - would upload {len(items)} file(s):")
        for _, key, _ in items:
            print(f"  {key}")
        return

    store = R2Store.from_env("Set them (see LAUNCH.md) or use --dry-run.")
    if store is None:
        sys.exit(2)
    uploaded, vanished, failed = 0, [], []
    for path, key, ctype in items:
        if not path.exists():
            # File discovered earlier but removed before upload (concurrent cleanup / new run):
            # a benign race — skip it, don't crash the whole publish mid-loop.
            vanished.append(key)
            print(f"skipped   {key} (file no longer present)")
            continue
        try:
            body = path.read_bytes()
        except FileNotFoundError:             # vanished between exists() and read -> benign race, not a failure
            vanished.append(key)
            print(f"skipped   {key} (file vanished before upload)")
            continue
        err = None
        for attempt in range(3):          # 3 attempts (2s, 4s) on top of boto3's own retries
            try:
                store.put(key, body, ctype)
                err = None
                break
            except Exception as ex:
                err = ex
                if attempt < 2:
                    import time as _t
                    _t.sleep(2 * (attempt + 1))
        if err is None:
            uploaded += 1
            print(f"uploaded  {key}")
        else:
            failed.append(key)
            print(f"FAILED    {key} (after retries): {str(err)[:140]}", file=sys.stderr)
    summary = f"Done - {uploaded} uploaded"
    if vanished:
        summary += f", {len(vanished)} vanished"
    if failed:
        summary += f", {len(failed)} FAILED"
    print(summary + f" -> bucket '{store.bucket}'.")
    if failed:
        # Real upload errors are surfaced as a non-zero exit (the publish chain checks this),
        # but only AFTER every file was attempted — no more aborting on the first failure.
        sys.exit(1)


if __name__ == "__main__":
    main()
