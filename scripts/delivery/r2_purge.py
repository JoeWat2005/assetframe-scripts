"""Delete report objects from the private Cloudflare R2 bucket.

The companion to publish.py: publish.py UPLOADS edition files to R2; this DELETES them.
Used for a fresh-state reset (wipe every published report) or to remove a single
edition date. Safe by default — it lists what it WOULD delete and changes nothing
until you pass --yes.

Reads the same R2_* env vars as publish.py (and the engine repo's .env if present):
  R2_ACCOUNT_ID  R2_ACCESS_KEY_ID  R2_SECRET_ACCESS_KEY  R2_BUCKET

Usage:
  python -m scripts.delivery.r2_purge                 dry run — list every object, delete nothing
  python -m scripts.delivery.r2_purge --yes           DELETE every object in the bucket
  python -m scripts.delivery.r2_purge --prefix 2026-06-22/   only that edition date (dry run)
  python -m scripts.delivery.r2_purge --prefix 2026-06-22/ --yes   delete just that date
"""
import argparse
import sys

from _r2 import R2Store          # shared env-load + boto3 client (deduped with publish.py)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--yes", action="store_true", help="actually delete (default: dry run)")
    ap.add_argument("--prefix", default="", help="only objects under this key prefix (e.g. 2026-06-22/)")
    a = ap.parse_args()

    store = R2Store.from_env("Set them (see LAUNCH.md) or run from the box where .env lives.")
    if store is None:
        sys.exit(2)

    # Page through every key (R2 returns <=1000 per page) so large buckets are fully covered.
    keys = store.list_keys(a.prefix)

    scope = f"prefix '{a.prefix}'" if a.prefix else "the ENTIRE bucket"
    if not keys:
        print(f"No objects found under {scope} in '{store.bucket}'. Nothing to do.")
        return

    if not a.yes:
        print(f"DRY RUN — {len(keys)} object(s) under {scope} would be deleted from "
              f"'{store.bucket}':")
        for k in keys[:50]:
            print(f"  {k}")
        if len(keys) > 50:
            print(f"  ... and {len(keys) - 50} more")
        print("\nRe-run with --yes to delete.")
        return

    deleted, failed = store.delete(keys)
    print(f"Deleted {deleted}/{len(keys)} object(s) from '{store.bucket}' (scope: {scope}).")
    if failed:
        print(f"{len(failed)} FAILED:", file=sys.stderr)
        for f in failed[:20]:
            print(f"  {f}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
