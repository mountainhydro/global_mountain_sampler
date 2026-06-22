"""
Download a CHELSA dataset described by a Cyberduck (.duck) bookmark.

A .duck file is a plist (XML) describing an S3 connection. This script reads
the bookmark, opens an anonymous S3 connection to the same endpoint/bucket/path,
lists the objects underneath, and downloads them to a local directory while
preserving the key structure.

Requirements
------------
    pip install boto3      # (already in the `gee` conda env)

Usage
-----
    # List what is available (no download)
    python download_chelsa_duck.py "C:/.../envicloud.duck" --dry-run

    # Download everything under the bookmark's path into ./chelsa_download
    python download_chelsa_duck.py "C:/.../envicloud.duck" -o ./chelsa_download

    # Only the bio variables for the 1981-2010 period, 8 parallel workers
    python download_chelsa_duck.py "C:/.../envicloud.duck" -o ./out \
        --include bio 1981-2010 --workers 8

    # Just grab the first 3 files (handy smoke test)
    python download_chelsa_duck.py "C:/.../envicloud.duck" -o ./out --limit 3
"""

from __future__ import annotations

import argparse
import concurrent.futures
import plistlib
import sys
from pathlib import Path

import boto3
from botocore import UNSIGNED
from botocore.config import Config
from botocore.exceptions import ClientError

# SWITCH object-store endpoints to fall back to when the bookmark's own host
# does not actually serve the bucket. The chelsa02 bucket currently lives on
# os.unil even though some .duck bookmarks still point at os.zhdk.
FALLBACK_ENDPOINTS = [
    "https://os.unil.cloud.switch.ch",
    "https://os.zhdk.cloud.switch.ch",
]


# ── Bookmark parsing ────────────────────────────────────────────────────────────

def parse_duck(duck_path: Path) -> dict:
    """Read a Cyberduck .duck bookmark and return connection details.

    Returns dict with: endpoint, bucket, prefix, anonymous (bool).
    """
    with open(duck_path, "rb") as f:
        data = plistlib.load(f)

    protocol = (data.get("Protocol") or "").lower()
    if protocol != "s3":
        raise ValueError(f"Only S3 bookmarks are supported (got Protocol={protocol!r}).")

    hostname = data["Hostname"]
    port     = str(data.get("Port", "443"))
    scheme   = "https" if port == "443" else "http"
    endpoint = f"{scheme}://{hostname}"

    # Path looks like "/<bucket>/<prefix...>/". First segment is the bucket.
    path_parts = (data.get("Path") or "/").strip("/").split("/")
    bucket = path_parts[0]
    prefix = "/".join(path_parts[1:])
    if prefix and not prefix.endswith("/"):
        prefix += "/"

    username  = (data.get("Username") or "").lower()
    anonymous = username in ("", "anonymous")

    return {
        "endpoint":  endpoint,
        "bucket":    bucket,
        "prefix":    prefix,
        "anonymous": anonymous,
        "nickname":  data.get("Nickname", ""),
    }


# ── S3 helpers ──────────────────────────────────────────────────────────────────

def make_s3(conn: dict, endpoint: str | None = None):
    cfg_kwargs = {"s3": {"addressing_style": "path"}}
    if conn["anonymous"]:
        cfg_kwargs["signature_version"] = UNSIGNED
    return boto3.client(
        "s3",
        endpoint_url=endpoint or conn["endpoint"],
        config=Config(**cfg_kwargs),
    )


def bucket_served_by(conn: dict, endpoint: str) -> bool:
    """True if `endpoint` actually serves the bucket (a quick 1-key probe)."""
    try:
        make_s3(conn, endpoint).list_objects_v2(
            Bucket=conn["bucket"], Prefix=conn["prefix"], MaxKeys=1
        )
        return True
    except ClientError:
        return False


def resolve_endpoint(conn: dict, override: str | None = None) -> str:
    """Pick an endpoint that actually serves the bucket.

    Order: explicit --endpoint override, then the bookmark's own host, then the
    known SWITCH fallbacks. Raises RuntimeError if none work.
    """
    candidates = []
    for ep in [override, conn["endpoint"], *FALLBACK_ENDPOINTS]:
        if ep and ep not in candidates:
            candidates.append(ep)
    for ep in candidates:
        if bucket_served_by(conn, ep):
            return ep
    raise RuntimeError(
        f"Bucket {conn['bucket']!r} not reachable on any of: {', '.join(candidates)}"
    )


def list_objects(s3, bucket: str, prefix: str,
                 includes: list[str], vars_filter: list[str]) -> list[dict]:
    """List all objects under prefix. Returns dicts: key, size.

    Filters (combined with AND):
      - vars_filter: keep only keys whose variable folder (first path segment
        after the prefix, e.g. "tas", "pr") is one of these — exact match.
      - includes: every token must appear somewhere in the key (substring AND).
    """
    paginator = s3.get_paginator("list_objects_v2")
    out = []
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if key.endswith("/"):
                continue  # skip "folder" placeholder keys
            var = key[len(prefix):].split("/")[0] if key.startswith(prefix) else ""
            if vars_filter and var not in vars_filter:
                continue
            if includes and not all(tok in key for tok in includes):
                continue
            out.append({"key": key, "size": obj["Size"]})
    return out


def download_one(conn: dict, bucket: str, prefix: str, obj: dict, out_dir: Path) -> str:
    """Download a single object, preserving its path relative to `prefix`."""
    key = obj["key"]
    rel = key[len(prefix):] if key.startswith(prefix) else key
    dest = out_dir / rel
    dest.parent.mkdir(parents=True, exist_ok=True)

    # Skip if already fully downloaded (size match).
    if dest.exists() and dest.stat().st_size == obj["size"]:
        return f"SKIP (exists)  {rel}"

    s3 = make_s3(conn)  # one client per worker thread
    tmp = dest.with_suffix(dest.suffix + ".part")
    s3.download_file(bucket, key, str(tmp))
    tmp.replace(dest)
    return f"OK  {rel}  ({obj['size'] / 1e6:.1f} MB)"


# ── Main ────────────────────────────────────────────────────────────────────────

def human_gb(n_bytes: int) -> str:
    return f"{n_bytes / 1024**3:.2f} GB"


# Used when the script is launched with no `duck` argument (e.g. the IDE run button).
DEFAULT_DUCK = r"C:\Users\stefan\Projects\MountAInWater\Climate data\envicloud.duck"


def main() -> int:
    p = argparse.ArgumentParser(description="Download a dataset from a Cyberduck .duck S3 bookmark")
    p.add_argument("duck", nargs="?", default=DEFAULT_DUCK,
                   help=f"Path to the .duck bookmark file (default: {DEFAULT_DUCK})")
    p.add_argument("-o", "--out", default="./chelsa_download",
                   help="Output directory (default: ./chelsa_download)")
    p.add_argument("--vars", nargs="+", default=[], metavar="VAR",
                   help="Only download these variable folders, exact match "
                        "(e.g. --vars tas pr). Default: all variables. "
                        "Available: clt cmi hurs pet pr rsds sfcWind tas tasmax "
                        "tasmin vpd")
    p.add_argument("--period", default="1981-2010",
                   help="Climatology period to download (default: 1981-2010, the "
                        "historical baseline). Use 'all' to also include future "
                        "projections (2011-2040 etc.).")
    p.add_argument("--include", nargs="+", default=[], metavar="TOKEN",
                   help="Additionally require ALL of these substrings in the key "
                        "(e.g. --include GFDL-ESM4 ssp126)")
    p.add_argument("--dry-run", action="store_true",
                   help="List matching files only — no download")
    p.add_argument("--limit", type=int, default=None,
                   help="Download at most N files (smoke test)")
    p.add_argument("--workers", type=int, default=4,
                   help="Parallel download workers (default: 4)")
    p.add_argument("--endpoint", default=None,
                   help="Override the S3 endpoint URL (e.g. https://os.unil.cloud.switch.ch)")
    p.add_argument("-y", "--yes", action="store_true",
                   help="Skip the confirmation prompt before downloading")
    args = p.parse_args()

    duck_path = Path(args.duck)
    if not duck_path.exists():
        print(f"Bookmark not found: {duck_path}", file=sys.stderr)
        return 2

    conn = parse_duck(duck_path)
    print(f"Bookmark : {conn['nickname']}")
    print(f"Bucket   : {conn['bucket']}")
    print(f"Prefix   : {conn['prefix']}")
    print(f"Auth     : {'anonymous' if conn['anonymous'] else 'credentials required'}")

    try:
        endpoint = resolve_endpoint(conn, args.endpoint)
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        return 2
    if endpoint != conn["endpoint"]:
        print(f"Endpoint : {endpoint}  (bookmark host {conn['endpoint']} not serving bucket)")
    else:
        print(f"Endpoint : {endpoint}")
    conn["endpoint"] = endpoint

    # The period is just another required substring; "all" disables the filter.
    includes = list(args.include)
    if args.period and args.period.lower() != "all":
        includes.append(args.period)

    s3 = make_s3(conn)
    print("\nListing objects …")
    objs = list_objects(s3, conn["bucket"], conn["prefix"], includes, args.vars)
    objs.sort(key=lambda o: o["key"])
    if args.limit:
        objs = objs[: args.limit]

    total = sum(o["size"] for o in objs)
    print(f"Found {len(objs)} files  ({human_gb(total)} total)")

    if not objs:
        print("Nothing to download.")
        return 0

    if args.dry_run:
        print("\n-- DRY RUN — no files will be downloaded --")
        for o in objs[:30]:
            rel = o["key"][len(conn["prefix"]):]
            print(f"  {rel}  ({o['size'] / 1e6:.1f} MB)")
        if len(objs) > 30:
            print(f"  … and {len(objs) - 30} more")
        return 0

    out_dir = Path(args.out)
    if not args.yes:
        prompt = f"\nDownload {len(objs)} files ({human_gb(total)}) to {out_dir.resolve()}? [y/N] "
        try:
            if input(prompt).strip().lower() != "y":
                print("Aborted.")
                return 0
        except EOFError:
            print("\nNo interactive input available — pass --yes to download, "
                  "or --dry-run to just list. Aborted.")
            return 0

    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"\nDownloading to {out_dir.resolve()} with {args.workers} workers …\n")

    ok = skip = err = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(download_one, conn, conn["bucket"], conn["prefix"], o, out_dir): o
            for o in objs
        }
        for i, fut in enumerate(concurrent.futures.as_completed(futures), 1):
            o = futures[fut]
            try:
                status = fut.result()
                if status.startswith("SKIP"):
                    skip += 1
                else:
                    ok += 1
                print(f"[{i:4d}/{len(objs)}] {status}")
            except Exception as exc:
                err += 1
                print(f"[{i:4d}/{len(objs)}] ERROR  {o['key']}  — {exc}", file=sys.stderr)

    print(f"\nDone.  OK={ok}  Skipped={skip}  Errors={err}")
    return 1 if err else 0


if __name__ == "__main__":
    sys.exit(main())
