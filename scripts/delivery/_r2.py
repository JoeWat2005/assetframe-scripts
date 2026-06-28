"""scripts/delivery/_r2.py — shared R2 (S3-compatible, boto3) client wrapper.

Folds the env-load + missing-var check + boto3 client build that were duplicated in publish.py +
r2_purge.py into one place. Behaviour is byte-identical to the old inline code (those CLIs have no
unit tests, so this is a verbatim extraction)."""
import os
import sys

from _paths import ROOT

_R2_KEYS = ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET")


def _load_local_env():
    """Populate missing R2_* vars from the engine repo's .env so the CLIs work without exporting."""
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


class R2Store:
    """Thin wrapper over a boto3 S3 client pointed at the private R2 bucket."""

    def __init__(self, client, bucket):
        self.client = client
        self.bucket = bucket

    @classmethod
    def from_env(cls, missing_hint):
        """Build from the R2_* env (+ .env). Returns None — after printing the byte-identical stderr
        message — when creds or boto3 are missing, so the caller does `sys.exit(2)`."""
        _load_local_env()
        env = {k: os.environ.get(k, "") for k in _R2_KEYS}
        missing = [k for k, v in env.items() if not v]
        if missing:
            print("Missing environment variables: " + ", ".join(missing) + "\n" + missing_hint,
                  file=sys.stderr)
            return None
        try:
            import boto3  # noqa
        except ImportError:
            print("boto3 is required:  pip install boto3", file=sys.stderr)
            return None
        client = boto3.client(
            "s3",
            endpoint_url=f"https://{env['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com",
            aws_access_key_id=env["R2_ACCESS_KEY_ID"],
            aws_secret_access_key=env["R2_SECRET_ACCESS_KEY"],
            region_name="auto",
        )
        return cls(client, env["R2_BUCKET"])

    def put(self, key, body, content_type):
        self.client.put_object(Bucket=self.bucket, Key=key, Body=body, ContentType=content_type)

    def list_keys(self, prefix=""):
        """Every key in the bucket (optionally under `prefix`), paging through <=1000/page."""
        keys, token = [], None
        while True:
            kw = {"Bucket": self.bucket, "MaxKeys": 1000}
            if prefix:
                kw["Prefix"] = prefix
            if token:
                kw["ContinuationToken"] = token
            resp = self.client.list_objects_v2(**kw)
            for obj in resp.get("Contents", []) or []:
                keys.append(obj["Key"])
            if resp.get("IsTruncated"):
                token = resp.get("NextContinuationToken")
            else:
                break
        return keys

    def delete(self, keys):
        """Delete keys in <=1000-key batches. Returns (deleted_count, [failure strings])."""
        deleted, failed = 0, []
        for i in range(0, len(keys), 1000):
            batch = [{"Key": k} for k in keys[i:i + 1000]]
            resp = self.client.delete_objects(Bucket=self.bucket,
                                               Delete={"Objects": batch, "Quiet": True})
            deleted += len(batch) - len(resp.get("Errors", []) or [])
            for e in resp.get("Errors", []) or []:
                failed.append(f"{e.get('Key')}: {e.get('Message')}")
        return deleted, failed
