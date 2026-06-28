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
import os
import sys
from pathlib import Path

from _paths import ROOT          # repo-root anchor (scripts/__init__ shim is on sys.path under -m)


def _load_local_env():
    """Populate missing R2_* vars from the engine repo's .env (same as publish.py)."""
    envfile = ROOT / ".env"
    if not envfile.exists():
        return
    for line in envfile.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip()
        if k.startswith("R2_") and not os.environ.get(k):
            os.environ[k] = v


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--yes", action="store_true", help="actually delete (default: dry run)")
    ap.add_argument("--prefix", default="", help="only objects under this key prefix (e.g. 2026-06-22/)")
    a = ap.parse_args()

    _load_local_env()
    env = {k: os.environ.get(k, "") for k in
           ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET")}
    missing = [k for k, v in env.items() if not v]
    if missing:
        print("Missing environment variables: " + ", ".join(missing) +
              "\nSet them (see LAUNCH.md) or run from the box where .env lives.", file=sys.stderr)
        sys.exit(2)

    try:
        import boto3  # noqa
    except ImportError:
        print("boto3 is required:  pip install boto3", file=sys.stderr)
        sys.exit(2)

    client = boto3.client(
        "s3",
        endpoint_url=f"https://{env['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com",
        aws_access_key_id=env["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=env["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
    )

    # Page through every key (R2 returns <=1000 per page) so large buckets are fully covered.
    keys = []
    token = None
    while True:
        kw = {"Bucket": env["R2_BUCKET"], "MaxKeys": 1000}
        if a.prefix:
            kw["Prefix"] = a.prefix
        if token:
            kw["ContinuationToken"] = token
        resp = client.list_objects_v2(**kw)
        for obj in resp.get("Contents", []) or []:
            keys.append(obj["Key"])
        if resp.get("IsTruncated"):
            token = resp.get("NextContinuationToken")
        else:
            break

    scope = f"prefix '{a.prefix}'" if a.prefix else "the ENTIRE bucket"
    if not keys:
        print(f"No objects found under {scope} in '{env['R2_BUCKET']}'. Nothing to do.")
        return

    if not a.yes:
        print(f"DRY RUN — {len(keys)} object(s) under {scope} would be deleted from "
              f"'{env['R2_BUCKET']}':")
        for k in keys[:50]:
            print(f"  {k}")
        if len(keys) > 50:
            print(f"  ... and {len(keys) - 50} more")
        print("\nRe-run with --yes to delete.")
        return

    # delete_objects takes <=1000 keys per call.
    deleted, failed = 0, []
    for i in range(0, len(keys), 1000):
        batch = [{"Key": k} for k in keys[i:i + 1000]]
        resp = client.delete_objects(Bucket=env["R2_BUCKET"], Delete={"Objects": batch, "Quiet": True})
        deleted += len(batch) - len(resp.get("Errors", []) or [])
        for e in resp.get("Errors", []) or []:
            failed.append(f"{e.get('Key')}: {e.get('Message')}")

    print(f"Deleted {deleted}/{len(keys)} object(s) from '{env['R2_BUCKET']}' (scope: {scope}).")
    if failed:
        print(f"{len(failed)} FAILED:", file=sys.stderr)
        for f in failed[:20]:
            print(f"  {f}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
