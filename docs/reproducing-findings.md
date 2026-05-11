# Reproducing CADENCE findings

This document walks an independent reader through reproducing the dataset
and the headline finding from scratch. It pairs with
[`methodology.md`](methodology.md) (the *what* and *why*) and
[`data-dictionary.md`](data-dictionary.md) (the schema).

If something here doesn't match what your run produces and you can't find
the cause in [`threats-to-validity.md`](threats-to-validity.md), please
file an issue — that's the kind of variance worth understanding.

## Audience and scope

Anyone with a Linux host, ~50 GB of disk, and an internet connection can
reproduce CADENCE end-to-end. The data is entirely public and
unauthenticated; nothing here requires a Red Hat subscription, ROSA
account, or any private infrastructure.

The instructions below assume Fedora 42 to match the project's primary
target, but they work on any distribution that has Python 3.12+ and the
`rpm` package. macOS works for everything except NEVRA comparison (the
project's pure-Python `rpmvercmp` fallback in `cadence/analysis/nevra.py`
keeps the test suite running on macOS, but for citable findings use a
host with the real C bindings — see methodology §5).

## Step-by-step

```bash
# 1. Install (Fedora target)
sudo dnf install -y python3.12 python3-rpm skopeo
git clone https://github.com/north-echo/cadence
cd cadence
uv sync --extra dev --extra export

# 2. Initialise
cadence db init

# 3. Backfill
#    These are run sequentially because they share rate-limited HTTP. Total
#    wall-clock on the target host is dominated by step 3d (catalog
#    backfill) — see the runtime table below.
cadence collect rhsa     --since 2024-01-01     #  ~5 min
cadence collect csaf     --all-known            # ~30 min
cadence collect repodata                         #  ~5 min
cadence collect catalog                          #  ~3 h  (full UBI history)
cadence collect quay                             # ~20 min

# 4. Compute the gaps + intervals
cadence analyze reconstruct                       #  ~5 min

# 5. Verify a sample of catalog rows against the live registry
cadence verify random --sample 50                 #  ~2 min (needs skopeo)

# 6. Produce the publication bundle
mkdir -p out
cadence report charts   --output-dir out/charts
cadence report markdown --output     out/report.md
cadence export dataset  --output-dir out/dataset
cadence export raw      --output-file out/raw.tar.zst

# 7. Spot-check the dataset
python -c 'import pyarrow.parquet as pq; t = pq.read_table("out/dataset/cadence-dataset.parquet"); print(t.num_rows, "rows"); print(t.schema)'
jq -s 'length' out/dataset/cadence-dataset.json
```

## Expected runtime and resource consumption

On the project's target host (OptiPlex 7060 Micro: i7-8700T, 16 GB DDR4,
Fedora Server 42), with the default 1 req/sec per-host rate limit:

| Step | Wall clock | Notes |
|---|---|---|
| `collect rhsa --since 2024-01-01` | ~5 min | List endpoint + 1 detail per RHSA |
| `collect csaf --all-known` | ~30 min | One CSAF fetch per RHSA from §3 |
| `collect repodata` | ~5 min | 18 repos × ~MB each, parsed in stream |
| `collect catalog` (first run) | ~3 h | Full historical backfill; subsequent `--since` runs take minutes |
| `collect quay` | ~20 min | Per-tag manifest resolution; mostly cache-hit on re-runs |
| `analyze reconstruct` | ~5 min | Single-pass SQL with NEVRA comparator |
| `report charts` | ~30 s | 8 charts × matplotlib + plotly |
| `export dataset` | ~10 s | Parquet + CSV + JSONL |
| `export raw` | ~30 s | tar.zst over the raw_json columns |

Disk:

- Database (`~/.local/share/cadence/cadence.db`): ~1-2 GB after the full
  backfill, growing slowly with forward collection.
- HTTP cache (`~/.cache/cadence/`): up to ~3 GB; safe to delete and re-warm.
- Published dataset: ~50-100 MB depending on data volume.

CPU and memory are bounded by what `iterparse` and `pyarrow.parquet.write`
need; both are stream-friendly. Peak RSS stays under 1 GB.

Network: outbound only, polite-rate-limited per [`methodology.md`](methodology.md)
§3. The backfill makes tens of thousands of HTTPS requests over the wall
clock; nothing reaches authenticated endpoints.

## Reproducing the headline finding

> *"Layered Red Hat products are the bottleneck."*
> Median Gap C: UBI ≈ OCP platform fast (5-10 days); RHACM, MCE, Service
> Mesh slow (16-26 days); Quay-hosted content variable.

After step 4 above:

```bash
cadence analyze gaps --gap C --slice-by tier
```

You should see medians ordered roughly as `ubi < ocp_platform <
quay_community < rh_layered`. The chart that shows this directly is
`out/charts/headline_gap_c_by_tier.png` (produced in step 6).

If your numbers materially disagree (say, by more than ~30% on the median
of a single tier), the most common causes are:

1. **Forward-collection window too short.** Gap A is forward-only;
   a single weekend of data is dominated by NULLs (see methodology §6).
   Wait for 30+ days of polling before drawing conclusions.
2. **Methodology drift.** If you've changed `cadence/analysis/reconstruct.py`,
   bump `--methodology-version` so your run coexists with the published
   one rather than overwriting it.
3. **Repository set drift.** If `cadence/targets.py` has changed since the
   published dataset, your tier composition is different. The dataset's
   `manifest.json` records the cadence version it was produced with.

## Sources of variance between runs

Runs of `cadence analyze reconstruct` on the same database are
deterministic at a given methodology version (DELETE+INSERT keyed by
version; full-table replace for intervals). Variance enters at the
*collection* stage, where the live world keeps moving:

* **New tags between two collection windows.** Each `collect catalog`
  / `collect quay` run sees more tags than the previous one. Gap C
  measurements on RHSAs published *during* the latest collection
  window can move (and tighten) as new images are observed.
* **Catalog ingestion lag.** A build may be present on
  `registry.access.redhat.com` minutes before the catalog API exposes
  it; CADENCE measures the catalog's view, not the registry's. See
  threats-to-validity §4.
* **Polling jitter.** `RandomizedDelaySec=10min` on the systemd timers
  spreads load; consecutive Gap A values for the same fix on different
  hosts can differ by up to that jitter window.

The published dataset's `manifest.json` records the time range of the
RHSAs it covers and the CADENCE version used. To reproduce the
*published* numbers exactly:

1. Check out the same CADENCE tag.
2. Re-run collection over the same `--since`/`--until` range.
3. Use the same `methodology_version`.

The raw archive (`cadence export raw`) is byte-for-byte deterministic
for the same database, so two reconstructions from the same archive
will produce identical artefacts.
