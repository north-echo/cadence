# CADENCE Data Dictionary

> WP-10 cut. The schema reference is fleshed out for every column that ships
> data today; per-table fully-rounded definitions land with WP-13.

All timestamps are stored as **ISO 8601 strings with timezone offsets**, UTC
unless otherwise noted (e.g., `2025-01-30T18:06:01+00:00`). Durations
(`*_seconds`) are integers in seconds.

NEVRA-bearing columns (`version`, `fixed_version`) store the canonical
`{epoch}:{version}-{release}` form. Architecture is kernel-arch
(`x86_64`, `aarch64`, `noarch`, `src`) throughout, even for Quay data
collected under Docker/OCI vocabulary (translated at the URL boundary; see
`cadence/collectors/catalog.py` and `cadence/collectors/quay.py`).

## Schema reference

### `rhsa`

One row per Red Hat Security Advisory. Populated by WP-03.

| Column | Type | Description |
|---|---|---|
| `rhsa_id` | TEXT PK | `RHSA-YYYY:NNNN` |
| `title` | TEXT | Document title from CSAF |
| `severity` | TEXT | Lowercase: `critical`/`important`/`moderate`/`low` |
| `published_at` | TIMESTAMP | `document.tracking.initial_release_date` |
| `updated_at` | TIMESTAMP | `document.tracking.current_release_date` |
| `source_url` | TEXT | Where the document was fetched from |
| `raw_json` | TEXT | Verbatim CSAF document for re-analysis |
| `collected_at` | TIMESTAMP | When CADENCE pulled it |

### `rhsa_cve`, `rhsa_package_fix`, `rhsa_vex`

Three child tables for `rhsa`. `rhsa_package_fix.fixed_version` is in the
canonical EVR format. `rhsa_vex.status` is one of `fixed`, `affected`,
`not_affected`, `under_investigation` (see `cadence/collectors/csaf.py`
for the CSAF-to-CADENCE status mapping).

### `repo_observation`, `repo_package`

Forward-only UBI repodata observations. Populated by WP-05.
`repo_observation.repomd_revision` is the upstream-reported revision (used
for idempotent skipping when nothing has changed).

### `container_image`, `container_image_rpm`

Per-image rows from both the Red Hat Container Catalog (WP-06) and Quay
(WP-07). `source ∈ {catalog, quay}`. Quay rows carry no
`container_image_rpm` records in v1 (see `methodology.md` §11).
`parsed_version` / `parsed_build_num` are populated when the tag matches
`X[.Y[.Z]]-NNN`; otherwise NULL.

### `catalog_advisory_mapping`

Pre-November-2024 legacy advisory mapping captured from the catalog
`comparison.advisory_rpm_mapping` field, used as a cross-validation
signal in WP-09. Empty for post-Nov-2024 images.

### `tracked_repository`

The Section-6 repo set that drives both `cadence collect …` and
`cadence analyze reconstruct`. Seeded on every collector run from
`cadence/targets.py`. `rationale` documents *why* the repo is tracked, so
the dataset's selection bias is auditable from the database alone.

### `gap_measurement`

Output of `cadence analyze reconstruct` (WP-09). One row per
`(rhsa_id, repository, architecture, package_name, fixed_version,
methodology_version)`. `image_id` references the earliest image that
carries the fix (NULL when not observed). `gap_{a,b,c}_seconds` are the
durations defined in `methodology.md` §6; any may be NULL.

### `rebuild_interval`

Output of `cadence analyze reconstruct` (WP-09). One row per consecutive
pair of `container_image` rows within a `(repository, architecture)`
group. `interval_seconds` is the wall-clock distance between consecutive
`build_date` values.

## `cadence analyze` output schema (WP-10)

`cadence analyze gaps` and `cadence analyze intervals` emit zero or more
**distribution rows**, one per slice. The same schema is rendered as a Rich
table (default), JSON, or CSV. All numeric values are durations in seconds
(integer for `count`, float otherwise). The schema is the same regardless
of which command produced it.

| Field | Type | Description |
|---|---|---|
| `facet` | string | The slice value (e.g. `ubi`, `critical`, `ubi9/ubi`, `2025-01`). The string `<overall>` is used when no `--slice-by` was supplied. |
| `count` | integer | Number of observations contributing to this slice. |
| `mean` | float | Arithmetic mean. NULL when `count == 0`. |
| `stddev` | float | Population standard deviation. `0.0` when `count == 1`. NULL when `count == 0`. |
| `median` | float | 50th percentile (linear interpolation). |
| `p25`, `p75`, `p90`, `p95`, `p99` | float | Percentiles via linear interpolation. |
| `low_n_warning` | bool | `true` when `count < 30`; warns the consumer that percentiles are unreliable. |

### JSON output

A JSON array of objects with the fields above. Field order matches the
table above. `null` is used for missing values, lowercase `true`/`false`
for `low_n_warning`.

```jsonc
[
  {
    "facet": "ubi",
    "count": 5,
    "mean": 5.0,
    "stddev": 0.0,
    "median": 5.0,
    "p25": 5.0,
    "p75": 5.0,
    "p90": 5.0,
    "p95": 5.0,
    "p99": 5.0,
    "low_n_warning": true
  }
]
```

### CSV output

Same fields, header row first. Floats are formatted with two decimal
places. `low_n_warning` is rendered as `yes` or empty string. Empty cells
indicate a NULL value (only possible when `count == 0`).

```csv
facet,count,mean,stddev,median,p25,p75,p90,p95,p99,low_n_warning
ubi,5,5.00,0.00,5.00,5.00,5.00,5.00,5.00,5.00,yes
```

### Supported facets (`--slice-by`)

| Facet | Gaps | Intervals | Description |
|---|---|---|---|
| `tier` | yes | yes | ubi / ocp_platform / rh_layered / quay_* |
| `severity` | yes | — | RHSA severity (intervals are image-level) |
| `repository` | yes | yes | Container repository |
| `ubi_major` | yes | yes | 8 / 9 / 10 (derived from repository) |
| `architecture` | yes | yes | Kernel arch |
| `package` | yes | — | Fixed package name; combine with `--top N` |
| `month` | yes | yes | Calendar `YYYY-MM` of RHSA pub / next build |
| `dow` | yes | yes | Day-of-week (SQLite `strftime('%w', …)`) |
| `dom` | yes | yes | Day-of-month |
