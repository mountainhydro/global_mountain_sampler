# Global Mountain Sampler

A pipeline for drawing a globally representative sample of the world's mountains
from the [AlphaEarth Satellite Embeddings](https://developers.google.com/earth-engine/datasets/catalog/GOOGLE_SATELLITE_EMBEDDING_V1_ANNUAL),
and for inspecting where that sample sits in climate space. Everything heavy runs
on **Google Earth Engine**; only small summaries and the final plots come back
locally.

## What it does

1. **Sample** all mountain ranges in the **GMBA Inventory**, stratified by
   elevation (Kapos classes) and by region, drawing ~50,000 points.
2. **Reduce** that pool to **1,000 representative points (medoids)** via k-means
   over the 64 AlphaEarth embedding dimensions — chosen to capture maximum
   diversity in topography, land cover and climate.
3. **Visualise** the 1,000 points on a global map and against the mountain
   **climate envelope** (CHELSA), with selected supersite basins highlighted.

The 1,000 medoids are the deliverable: a compact set of sites that spans the
breadth of global mountain environments.

## Components

| File | Role |
|------|------|
| `notebooks/global_sampling.ipynb` | Stratified GEE sampling (50k) → k-means medoid reduction (1000) → GEE assets |
| `notebooks/global_climate_space.ipynb` | Annual CHELSA climate → climate-space density plots + interactive explorer → image/polygon assets for the app |
| `gee/global_sample_app.js` | Earth Engine App: global map of the 1000 medoids + GMBA regions + supersite basins, with on-the-fly climate-space density plots |
| `CHELSA_download/download_chelsa_duck.py` | Download CHELSA v2.1 GeoTIFFs from SWITCH ScienceCloud S3 to a local directory |
| `CHELSA_download/chelsa_to_gee.py` | Upload local CHELSA TIFs to GCS and ingest them as GEE Image assets |

## Method

**Sampling (`global_sampling`).** Mountains are defined as the interior of the
GMBA polygons. Each pixel is assigned a Kapos elevation class (K1 ≥4500 m … K6
300–1000 m) from a global DEM. The 50k sample is allocated:

- *geographically* — per region, proportional to `area ** 0.5`;
- *by elevation* — within each region, per class, proportional to `class_area ** 0.5`.

The square-root weighting deliberately over-samples small regions and rare
(high-alpine) classes so the pool spans the full diversity rather than being
dominated by a few huge ranges. Sampling uses `ee.Image.stratifiedSample` per
region and is exported as a GEE asset **and** a Drive CSV.

**Reduction.** The 1,000 medoids are selected by k-means on the 64 embedding
dimensions, run **per Kapos class** (per-class quota `count ** 0.5`, summing to
1000). Each cluster contributes the real point nearest its centroid. Per-class
clustering guarantees elevation representativity; geographic spread is inherited
from the already geo-stratified pool.

**Climate space (`global_climate_space`).** Monthly CHELSA v2.1 climatologies are
aggregated to annual variables (temperature, precipitation, PET, humidity, etc.),
masked to GMBA. For every pair of variables a 2-D density (hexbin) of the sample
is rendered, with the 1,000 medoids on top (coloured by Kapos class) and a **90%
climate hull** for each supersite basin. The plots are pre-rendered (GEE cannot
draw interactive 2-D densities) and ingested as image assets the app displays.

## Required input datasets

The pipelines read the following before producing anything. Public catalog
datasets need no setup; the others must exist in your project before running.

| Dataset | Used by | Type / how to provide |
|---------|---------|-----------------------|
| `GOOGLE/SATELLITE_EMBEDDING/V1/ANNUAL` (AlphaEarth) | `global_sampling` | Public GEE catalog — no setup |
| `USGS/GMTED2010` (global DEM, Kapos classes) | `global_sampling` | Public GEE catalog — no setup |
| `…/assets/GMBA_Inventory_standard_300` (mountain regions) | both notebooks + app | **Project asset** — ingest `GMBA_Inventory_v2.0_standard_300/*.shp` as a FeatureCollection table |
| `…/assets/chelsa_climatologies/1981-2010/<var>/CHELSA_<var>_<MM>_1981-2010_V21` | `global_climate_space` | **Project assets** — 132 monthly CHELSA v2.1 rasters (11 variables × 12 months) ingested as images; use `CHELSA_download/download_chelsa_duck.py` + `CHELSA_download/chelsa_to_gee.py` (see *Ingesting CHELSA into GEE* below) |
| Supersite basin shapefiles (`Supersites/.../*.shp`) | `global_climate_space` §5 | **Local files** — paths set in the `SUPERSITES` config |

Within the pipeline, `global_climate_space` and the app also consume assets that
`global_sampling` produces (`global_mountain_sample`, `global_mountain_sample_1000`),
so run `global_sampling` first. A staging **GCS bucket** (`GCS_BUCKET`) is also
needed for the climate-space plot ingestion.

## GEE assets produced

| Asset | Contents |
|-------|----------|
| `…/assets/global_mountain_sample` | the full ~50k stratified sample |
| `…/assets/global_mountain_sample_1000` | the 1000 medoids (final sample) |
| `…/assets/climate_space_plots/{x}__{y}` | 110 pre-rendered density plots (RGB) |
| `…/assets/supersites` | supersite basin polygons (Pamir, Riosanta, Vilcanota) |

(All under `projects/promising-era-496715-j5/assets/`. The GMBA inventory and the
monthly CHELSA climatologies are pre-existing assets in the same project.)

## How to run

1. Create/activate the `gee` environment and install deps:
   `pip install -r requirements.txt`, then authenticate once with `earthengine authenticate`.
2. **`global_sampling.ipynb`** — run top to bottom; after section 6 wait for the
   Drive CSV, download it to `data/`, then run sections 7–8 to produce the 1000
   medoids asset.
3. **`global_climate_space.ipynb`** — run sections 1–3; download the background
   Drive CSV to `data/`; run section 4 (explorer) and sections 5–6 (render +
   ingest the app plots and the supersites asset).
4. **`gee/global_sample_app.js`** — paste into the [EE Code Editor](https://code.earthengine.google.com)
   and run. To publish: the app's assets must be readable by viewers
   (`earthengine acl set public <asset>` for each, or grant the relevant readers).

## Ingesting CHELSA into GEE

The `CHELSA_download/` folder contains two standalone scripts for bringing the
CHELSA v2.1 climatologies into a GEE project. Run them **once**, before
`global_climate_space.ipynb`.

### 1 — Download CHELSA locally (`CHELSA_download/download_chelsa_duck.py`)

Downloads CHELSA GeoTIFFs from the SWITCH ScienceCloud S3 store to a local
directory. **No account, credentials, or extra tools are needed** — the script
connects anonymously using built-in defaults.

```bash
cd CHELSA_download

# Dry-run — list what would be downloaded (no files transferred)
python download_chelsa_duck.py --dry-run

# Download the 11-variable 1981-2010 climatologies (~15 GB)
python download_chelsa_duck.py \
    --vars clt cmi hurs pet pr rsds sfcWind tas tasmax tasmin vpd \
    --period 1981-2010

# Grab just the first 3 files (quick connectivity check)
python download_chelsa_duck.py --limit 3 --dry-run
```

Files land in `CHELSA_download/CHELSA_download/<var>/1981-2010/`.
Available variables: `clt cmi hurs pet pr rsds sfcWind tas tasmax tasmin vpd`
(and `bio` bioclimatic indices if you need them).

> **Optional `.duck` bookmark.** If you want to point the script at a different
> S3 source, pass a [Cyberduck](https://cyberduck.io/) `.duck` bookmark as the
> first positional argument: `python download_chelsa_duck.py path/to/my.duck`.

### 2 — Upload to GCS and ingest into GEE (`CHELSA_download/chelsa_to_gee.py`)

Streams each TIF from the CHELSA S3 bucket to a GCS staging bucket, then submits
a GEE ingest task. Requires a GCS bucket and a GEE project with asset write
access.

**Authentication** (one-time):
```bash
gcloud auth application-default login   # GCS uploads
earthengine authenticate                 # GEE ingestion
```

```bash
cd CHELSA_download

# Dry-run — list what would be uploaded/ingested, create nothing
python chelsa_to_gee.py \
    --project  my-gcp-project-id \
    --gcs-bucket  my-chelsa-bucket \
    --dry-run

# First run: let the script create the GCS bucket automatically, then ingest
python chelsa_to_gee.py \
    --project  my-gcp-project-id \
    --gcs-bucket  my-chelsa-bucket \
    --create-bucket \
    --period 1981-2010

# Custom GEE asset root (default: projects/<PROJECT>/assets/chelsa_climatologies)
python chelsa_to_gee.py \
    --project  my-gcp-project-id \
    --gcs-bucket  my-chelsa-bucket \
    --gee-base projects/my-gcp-project-id/assets/my_chelsa_folder \
    --period 1981-2010

# Skip the GCS upload if the TIFs are already staged there
python chelsa_to_gee.py \
    --project  my-gcp-project-id \
    --gcs-bucket  my-chelsa-bucket \
    --skip-gcs

# Non-interactive (CI / batch)
python chelsa_to_gee.py \
    --project  my-gcp-project-id \
    --gcs-bucket  my-chelsa-bucket \
    --yes
```

GEE assets land under `projects/<PROJECT>/assets/chelsa_climatologies/<period>/<var>/`
with one Image per monthly TIF, named after the CHELSA filename stem (dots stripped).
The `global_climate_space.ipynb` notebook expects this exact path structure.

## Repository layout

```
notebooks/          global_sampling.ipynb, global_climate_space.ipynb
gee/                global_sample_app.js
data/               pipeline inputs/outputs (CSVs, rendered plots, explorer HTML)
CHELSA_download/    scripts to download CHELSA and ingest into GEE;
                    downloaded TIFs live in CHELSA_download/CHELSA_download/
GMBA_Inventory_v2.0_standard_300/   GMBA shapefile (source of the GEE table asset)
alternatives/       superseded earlier pipeline (old notebooks, region app, helper
                    package, the standalone plot-render script, old outputs)
```
