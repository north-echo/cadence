# CADENCE

**Container Supply Chain Patch Latency Measurement**

CADENCE measures end-to-end patch latency in multi-hop container supply
chains. It polls public Red Hat data sources and computes the time between
RHSA publication and the appearance of fixed packages in successively
downstream container images.

## Why this exists

The container supply chain is multi-hop. A single security fix has to
traverse four (or more) independent rebuild boundaries before users see it:

1. **Hop 1.** A fix lands in RHEL and an RHSA is published.
2. **Hop 2.** UBI consumes from RHEL → new UBI base image.
3. **Hop 3.** A commercial managed service (OpenShift platform, RHACM, MCE,
   ODF, Logging, Service Mesh, …) rebuilds against the new UBI.
4. **Hop 4.** Quay-hosted community/partner content rebuilds on its own
   cadence.

Each hop has its own rebuild discipline. Cumulative latency is what users
actually feel. CADENCE measures each publicly-observable hop independently
and characterises the full distribution per tier.

Pre-flight analysis already established the headline shape:

- UBI rebuild cadence has accelerated materially in recent years (median
  ~5 days in the last 12 months, vs. ~12 days all-time).
- OpenShift platform images track UBI cadence closely (median ~7 days,
  p90 ~20 days).
- **Layered Red Hat products are the bottleneck.** RHACM, MCE, Service Mesh
  show median 16–26 days, p90 40–61 days, with individual gaps up to 91
  days.

CADENCE's role is to keep that picture honest, reproducible, and updated.

## Status

Pre-1.0. All collector and analysis work packages (WP-01 … WP-13) are
implemented; WP-14 (continuous-operation systemd units + cohabitation
notes) ships next. See [`CADENCE-SPEC.md`](CADENCE-SPEC.md) for the
project's full spec and work-package list, and [`NOTES.md`](NOTES.md) for
any places where reality has diverged from the spec.

## Installation

Python 3.12+ on a Linux host. The Fedora 42 target is what the project is
tested against; macOS works for development but the `rpm` extra is
Linux-only (see `NOTES.md`).

```bash
# Standard install
pip install ne-cadence

# With the dataset-export extras (parquet + tar.zst)
pip install 'ne-cadence[export]'

# Full dev install (uv-managed)
git clone https://github.com/north-echo/cadence && cd cadence
uv sync --extra dev
```

Or via the rootless Podman container:

```bash
podman build -t cadence .
podman run --rm -it \
    -v "$HOME/.local/share/cadence:/data:Z" \
    -v "$HOME/.cache/cadence:/cache:Z" \
    -e CADENCE_DB_PATH=/data/cadence.db \
    -e CADENCE_CACHE_DIR=/cache \
    cadence cadence --help
```

The container pins Fedora 42 and installs `python3-rpm` and `skopeo` from
the OS package set — the production path the project is designed for.

## Quickstart

```bash
# 1. Initialise the SQLite database (one-time)
cadence db init

# 2. Pull the raw data (each step takes seconds-to-minutes against the
#    live APIs; the full UBI backfill is a one-shot ~3-hour job).
cadence collect rhsa     --since 2025-01-01
cadence collect csaf     --all-known
cadence collect repodata
cadence collect catalog
cadence collect quay

# 3. Compute Gap A/B/C + inter-build intervals
cadence analyze reconstruct

# 4. Look at the numbers
cadence analyze gaps --slice-by tier
cadence analyze intervals --slice-by tier
cadence report summary

# 5. Produce a publication bundle
cadence report charts   --output-dir out/charts
cadence report markdown --output     out/report.md
cadence export dataset  --output-dir out/dataset
cadence export raw      --output-file out/raw.tar.zst
```

`cadence --help` lists every subcommand;
[`docs/reproducing-findings.md`](docs/reproducing-findings.md) walks through
a worked end-to-end run.

## Architecture overview

```
cadence/
├── cli.py                       # Click entry point
├── config.py                    # Pydantic Settings (XDG-aware)
├── db.py                        # SQLite migration runner (WAL mode)
├── targets.py                   # The Section-6 tracked-repo set
├── schema/                      # 001_initial.sql, future migrations
├── collectors/
│   ├── base.py                  # HTTPClient, RateLimiter, DiskCache
│   ├── rhsa.py                  # WP-03: Red Hat Security Data API
│   ├── csaf.py                  # WP-04: CSAF/VEX
│   ├── repodata.py              # WP-05: cdn-ubi (forward-only)
│   ├── catalog.py               # WP-06: Container Catalog API
│   ├── quay.py                  # WP-07: Quay.io (no RPM extraction v1)
│   └── registry.py              # WP-08: skopeo cross-check
├── analysis/
│   ├── nevra.py                 # rpm.labelCompare + Python fallback
│   ├── reconstruct.py           # WP-09: Gap A/B/C + cross-validation
│   ├── intervals.py             # WP-09: inter-build intervals
│   ├── slice.py                 # WP-10: faceted distributions
│   ├── gaps.py                  # WP-10: gap_distribution()
│   └── export.py                # WP-12: parquet/csv/jsonl + tar.zst
└── reports/
    ├── summary.py               # WP-11: Rich CLI summary
    ├── markdown.py              # WP-11: GitHub-renderable report
    └── charts.py                # WP-11: 8 publication charts
```

Every collector subclasses `BaseCollector` and shares one
`HTTPClient` (per-host rate limit, retry with jitter, on-disk cache).
Every persistence step is transactional and idempotent. Analyses are
versioned via `gap_measurement.methodology_version` so multiple
interpretations can coexist in the same database.

## Data and licensing

Every record CADENCE produces is derived from public data, with
`raw_json` columns preserving full upstream payloads for re-analysis. See
[`docs/methodology.md`](docs/methodology.md) for the analysis approach,
[`docs/data-dictionary.md`](docs/data-dictionary.md) for the schema and
exported-dataset shape, and
[`docs/threats-to-validity.md`](docs/threats-to-validity.md) for an honest
discussion of what the data does and doesn't support.

- Source code: [Apache-2.0](LICENSE)
- Produced dataset: [CC-BY-4.0](DATASET-LICENSE)

## Citation

If you use CADENCE data or methodology in research, please cite:

```bibtex
@misc{cadence2026,
  author       = {Lusk, Christopher},
  title        = {{CADENCE}: Container Supply Chain Patch Latency Measurement},
  year         = {2026},
  howpublished = {\url{https://github.com/north-echo/cadence}},
  note         = {Dataset: \url{https://github.com/north-echo/cadence}}
}
```

## Identity & disclosure

Every commit carries `Signed-off-by: Christopher Lusk
<clusk@northecho.dev>` and `Assisted-by: Claude (Anthropic)`. The
repository is part of the North Echo identity; it has no relationship to
any employer-internal system or compliance regime, and uses only public,
unauthenticated data sources.
