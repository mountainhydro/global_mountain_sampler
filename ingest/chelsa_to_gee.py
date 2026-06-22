"""
CHELSA Climatologies → Google Cloud Storage → GEE ImageCollection

Source  : S3-compatible object store at os.zhdk.cloud.switch.ch
Bucket  : chelsa02
Prefix  : chelsa/global/climatologies/
Access  : anonymous

Workflow
--------
1. List all GeoTIFFs in the S3 bucket (optionally filtered by period / variable)
2. Download each TIF to a local temp dir (or stream to GCS via pipe)
3. Upload to GCS
4. Ingest into GEE as individual Image assets, collecting all into one
   ImageCollection per variable group (bio, pr, tas, …)

Requirements
------------
    pip install boto3 google-cloud-storage earthengine-api

Authentication
--------------
    earthengine authenticate          # one-time, for GEE
    gcloud auth application-default login   # for GCS uploads

Usage
-----
    # Dry-run — just list what would be downloaded
    python chelsa_to_gee.py --dry-run

    # Download bio1–bio19 for both periods
    python chelsa_to_gee.py --vars bio

    # Full climatologies for 1981-2010 only
    python chelsa_to_gee.py --period 1981-2010

    # Everything (warning: ~150 GB+)
    python chelsa_to_gee.py --all
"""

import argparse
import concurrent.futures
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import boto3
import ee
import google.auth
import google.cloud.storage as gcs_lib
from botocore import UNSIGNED
from botocore.config import Config

# ── Configuration ──────────────────────────────────────────────────────────────

S3_ENDPOINT  = "https://os.unil.cloud.switch.ch"
S3_BUCKET    = "chelsa02"
S3_PREFIX    = "chelsa/global/climatologies/"

GCS_BUCKET   = "chelsa"                            # change to your GCS bucket
GCS_PREFIX   = "chelsa/climatologies"

GEE_PROJECT  = "promising-era-496715-j5"
GEE_BASE     = f"projects/{GEE_PROJECT}/assets/chelsa_climatologies"

# Parallel workers for download + upload
N_WORKERS    = 4

# Only these sub-folders are downloaded by default (--vars flag).
# Run --dry-run first to see the full folder list.
DEFAULT_VARS = [
    "bio",
    "pr",
    "tas",
    "tasmax",
    "tasmin",
    "vpd",
    "pet_penman",
]

PERIODS = ["1981-2010"]


# ── S3 helpers ─────────────────────────────────────────────────────────────────

def make_s3():
    return boto3.client(
        "s3",
        endpoint_url=S3_ENDPOINT,
        config=Config(signature_version=UNSIGNED, s3={"addressing_style": "path"}),
    )


def list_tifs(s3, prefix: str, period: str | None, vars_filter: list[str]) -> list[dict]:
    """
    Return list of dicts with keys: key, size_mb, period, var, filename.
    Applies optional period and variable filters.
    """
    paginator = s3.get_paginator("list_objects_v2")
    results = []
    for page in paginator.paginate(Bucket=S3_BUCKET, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if not key.lower().endswith(".tif"):
                continue
            parts = key.replace(S3_PREFIX, "").strip("/").split("/")
            if len(parts) < 3:
                continue
            v, p, fname = parts[0], parts[1], parts[-1]
            if period and p != period:
                continue
            if vars_filter and v not in vars_filter:
                continue
            results.append({
                "key":      key,
                "size_mb":  round(obj["Size"] / 1e6, 1),
                "period":   p,
                "var":      v,
                "filename": fname,
            })
    return results


# ── GCS helpers ────────────────────────────────────────────────────────────────

def gcs_uri(obj: dict) -> str:
    return f"gs://{GCS_BUCKET}/{GCS_PREFIX}/{obj['period']}/{obj['var']}/{obj['filename']}"


def _gcs_client():
    """GCS client reusing the earthengine credentials (avoids needing gcloud ADC)."""
    credentials = ee.data.get_persistent_credentials()
    return gcs_lib.Client(project=GEE_PROJECT, credentials=credentials)


def _gcs_blob(uri: str):
    """Return a GCS Blob object for the given gs:// URI."""
    path = uri[len(f"gs://{GCS_BUCKET}/"):]
    return _gcs_client().bucket(GCS_BUCKET).blob(path)


def gcs_exists(uri: str) -> bool:
    return _gcs_blob(uri).exists()


def upload_to_gcs(local_path: str, uri: str) -> None:
    _gcs_blob(uri).upload_from_filename(local_path)


# ── GEE helpers ────────────────────────────────────────────────────────────────

def init_ee():
    ee.Initialize(project=GEE_PROJECT)


def gee_asset_id(obj: dict) -> str:
    # GEE asset IDs may not contain dots, so "V.2.1" -> "V21".
    stem = Path(obj["filename"]).stem.replace(".", "")
    return f"{GEE_BASE}/{obj['period']}/{obj['var']}/{stem}"


def gee_asset_exists(asset_id: str) -> bool:
    try:
        ee.data.getAsset(asset_id)
        return True
    except ee.EEException:
        return False


def ensure_gee_folder(asset_id: str) -> None:
    """Create all parent folders in the GEE asset tree if they don't exist."""
    parts = asset_id.split("/")
    # asset IDs: projects/X/assets/chelsa_climatologies/period/var/stem
    # folders to ensure: assets root, period folder, var folder
    for depth in range(5, len(parts)):  # 0..4 = projects/X/assets/root/period
        folder = "/".join(parts[:depth])
        try:
            ee.data.getAsset(folder)
        except ee.EEException:
            ee.data.createAsset({"type": "Folder"}, opt_path=folder)


def ingest_to_gee(uri: str, asset_id: str, obj: dict) -> None:
    """Submit a GEE ingest task from GCS."""
    stem = Path(obj["filename"]).stem
    parts_fname = stem.split("_")
    band_name = "_".join(parts_fname[1:-2]) if len(parts_fname) >= 4 else stem

    # Modern ingestion manifest: destination in `id`, source(s) under
    # tilesets/sources/uris. Nodata is read from the GeoTIFF itself.
    manifest = {
        "id": asset_id,
        "tilesets": [{"sources": [{"uris": [uri]}]}],
        "bands": [{"id": band_name, "tilesetBandIndex": 0}],
        "pyramidingPolicy": "MEAN",
        "properties": {
            "period":    obj["period"],
            "variable":  obj["var"],
            "band_name": band_name,
            "source":    "CHELSA_V2.1",
        },
    }
    request_id = ee.data.newTaskId()[0]
    ee.data.startIngestion(request_id, manifest, allow_overwrite=True)


# ── Per-file pipeline ──────────────────────────────────────────────────────────

def process_one(obj: dict, dry_run: bool, skip_gcs: bool, skip_gee: bool) -> str:
    """Download → GCS → GEE for a single TIF. Returns status string."""
    uri      = gcs_uri(obj)
    asset_id = gee_asset_id(obj)
    label    = f"{obj['period']}/{obj['var']}/{obj['filename']}"

    if gee_asset_exists(asset_id):
        return f"SKIP (GEE exists)  {label}"

    if dry_run:
        return f"DRY-RUN  {label}  ({obj['size_mb']} MB)"

    # ── Step 1: upload to GCS ──
    if not skip_gcs and not gcs_exists(uri):
        with tempfile.TemporaryDirectory() as tmpdir:
            local_path = os.path.join(tmpdir, obj["filename"])
            s3 = make_s3()
            s3.download_file(S3_BUCKET, obj["key"], local_path)
            upload_to_gcs(local_path, uri)

    # ── Step 2: ingest into GEE ──
    if not skip_gee:
        ensure_gee_folder(asset_id)
        ingest_to_gee(uri, asset_id, obj)
        time.sleep(0.3)   # stay under GEE task-submit rate limit

    return f"OK  {label}"


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Ingest CHELSA climatologies from S3 into GEE via GCS"
    )
    parser.add_argument("--dry-run",    action="store_true",
                        help="List files only — no download, no upload")
    parser.add_argument("--all",        action="store_true",
                        help="Include all variable sub-folders (not just DEFAULT_VARS)")
    parser.add_argument("--vars",       nargs="+", metavar="VAR",
                        help=f"Variable folders to include (default: {DEFAULT_VARS})")
    parser.add_argument("--period",     choices=PERIODS + ["all"], default="all",
                        help="Climatology period (default: both)")
    parser.add_argument("--skip-gcs",   action="store_true",
                        help="Skip GCS upload (assume files already in GCS)")
    parser.add_argument("--skip-gee",   action="store_true",
                        help="Skip GEE ingest (upload to GCS only)")
    parser.add_argument("--limit",      type=int, default=None,
                        help="Process at most N files (useful for testing)")
    parser.add_argument("--workers",    type=int, default=N_WORKERS,
                        help=f"Parallel workers (default: {N_WORKERS})")
    parser.add_argument("--yes", "-y",  action="store_true",
                        help="Skip the confirmation prompt (for non-interactive runs)")
    args = parser.parse_args()

    period_filter = None if args.period == "all" else args.period
    vars_filter   = [] if args.all else (args.vars or DEFAULT_VARS)

    print(f"Connecting to {S3_ENDPOINT} …")
    s3   = make_s3()
    init_ee()
    tifs = list_tifs(s3, S3_PREFIX, period_filter, vars_filter)
    if args.limit:
        tifs = tifs[:args.limit]

    total_mb = sum(t["size_mb"] for t in tifs)
    print(f"\nFound {len(tifs)} TIF files  ({total_mb/1024:.1f} GB total)")

    # Summary by variable
    from collections import Counter
    by_var = Counter(t["var"] for t in tifs)
    for v, n in sorted(by_var.items()):
        print(f"  {v:20s}  {n:4d} files")

    if args.dry_run:
        print("\n-- DRY RUN — no files will be downloaded or uploaded --")
        for t in tifs[:20]:
            print(f"  {t['period']}/{t['var']}/{t['filename']}  ({t['size_mb']} MB)")
        if len(tifs) > 20:
            print(f"  … and {len(tifs)-20} more")
        return

    if not args.yes:
        try:
            confirm = input(f"\nProceed with {len(tifs)} files ({total_mb/1024:.1f} GB)? [y/N] ")
        except EOFError:
            confirm = ""
        if confirm.lower() != "y":
            print("Aborted.  (Pass --yes to run non-interactively.)")
            return

    # Ensure GEE root asset folder exists
    try:
        ee.data.getAsset(GEE_BASE)
    except ee.EEException:
        ee.data.createAsset({"type": "Folder"}, opt_path=GEE_BASE)

    ok = err = skip = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(process_one, obj, False, args.skip_gcs, args.skip_gee): obj
            for obj in tifs
        }
        for i, fut in enumerate(concurrent.futures.as_completed(futures), 1):
            try:
                status = fut.result()
                if status.startswith("SKIP"):
                    skip += 1
                else:
                    ok += 1
                print(f"[{i:4d}/{len(tifs)}] {status}")
            except Exception as exc:
                err += 1
                obj = futures[fut]
                print(f"[{i:4d}/{len(tifs)}] ERROR  {obj['period']}/{obj['var']}/{obj['filename']}  — {exc}",
                      file=sys.stderr)

    print(f"\nDone.  OK={ok}  Skipped={skip}  Errors={err}")


if __name__ == "__main__":
    main()
