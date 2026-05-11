# CADENCE Data Dictionary

This document describes every table in the SQLite database CADENCE writes
to, every column in those tables, and every field in the exported
artefacts produced by `cadence export`. External readers should be able to
load the published dataset and understand each value without consulting
the source code.

## Conventions

* **Timestamps** are ISO 8601 strings with explicit timezone offsets,
  always UTC. Example: `2025-01-30T18:06:01+00:00`. CADENCE never relies
  on the SQLite type system to coerce these; callers parse on read.
* **Durations** (`*_seconds`) are 64-bit integers, in seconds. The report
  and chart layers convert to days for display; storage stays in seconds.
* **NEVRA-bearing columns** (`version`, `fixed_version`) store the
  canonical `{epoch}:{version}-{release}` form. Compare with
  `cadence.analysis.nevra.evr_ge` / `label_compare` — never lexically.
* **Architecture** is the RPM ("kernel") vocabulary throughout:
  `x86_64`, `aarch64`, `noarch`, `src`. Quay data collected under the
  Docker/OCI vocabulary (`amd64`, `arm64`) is translated at the URL
  boundary; see `cadence/collectors/catalog.py` and
  `cadence/collectors/quay.py`.
* **Raw JSON** preserved in `raw_json` columns is the verbatim upstream
  payload, JSON-serialised with `sort_keys=True` so it's hash-stable
  across runs.
* **Idempotency** keys are documented per table.

## Schema reference

### `schema_migrations`

Internal bookkeeping for `cadence db migrate`. One row per applied
migration file (`name`, `applied_at`).

### `rhsa`

One row per Red Hat Security Advisory. Populated by `cadence collect rhsa`
(WP-03). Idempotency: UPSERT keyed on `rhsa_id`.

| Column | Type | Description |
|---|---|---|
| `rhsa_id` | TEXT PK | `RHSA-YYYY:NNNN` |
| `title` | TEXT | `document.title` from the CSAF document |
| `severity` | TEXT | Lowercase: `critical`, `important`, `moderate`, `low`, or `unknown` |
| `published_at` | TIMESTAMP | `document.tracking.initial_release_date` |
| `updated_at` | TIMESTAMP | `document.tracking.current_release_date` |
| `source_url` | TEXT | Where the document was fetched from (the CSAF v2 detail URL) |
| `raw_json` | TEXT | Verbatim CSAF v2 document, JSON-serialised with `sort_keys=True` |
| `collected_at` | TIMESTAMP | When CADENCE pulled it |

### `rhsa_cve`

CVE references for each RHSA. One row per `(rhsa_id, cve_id)`. Replaced
in full when the parent RHSA is re-collected.

| Column | Type | Description |
|---|---|---|
| `rhsa_id` | TEXT FK | References `rhsa.rhsa_id` |
| `cve_id` | TEXT | `CVE-YYYY-NNNNN` |
| `cvss3_score` | REAL | Base score from `vulnerabilities[].scores[].cvss_v3.baseScore` (nullable) |
| `cvss3_vector` | TEXT | CVSS3 vector string (nullable) |

### `rhsa_package_fix`

Fixed packages per RHSA. One row per `(rhsa_id, package_name,
fixed_version, arch, product)`. Replaced in full on re-collect.

| Column | Type | Description |
|---|---|---|
| `rhsa_id` | TEXT FK | References `rhsa.rhsa_id` |
| `package_name` | TEXT | RPM name (e.g. `python3-jinja2`) |
| `fixed_version` | TEXT | EVR: `{epoch}:{version}-{release}` (e.g. `0:2.11.3-6.el9_4`) |
| `arch` | TEXT | `x86_64`, `aarch64`, `noarch`, `src`, … |
| `product` | TEXT | CSAF product_id prefix (e.g. `AppStream-9.4.0.Z.EUS`) |

### `rhsa_vex`

VEX product statuses per RHSA. Populated by `cadence collect csaf` (WP-04).
One row per `(rhsa_id, product_id)`. Replaced in full on re-collect.

| Column | Type | Description |
|---|---|---|
| `rhsa_id` | TEXT FK | References `rhsa.rhsa_id` |
| `product_id` | TEXT | CSAF product_id (typically `PRODUCT:NVRA`) |
| `status` | TEXT | One of `fixed`, `affected`, `not_affected`, `under_investigation` (mapped from CSAF; see `cadence/collectors/csaf.py` for the source mapping) |
| `justification` | TEXT | First non-empty CSAF flag label matching this product (nullable) |

### `repo_observation`

One row per UBI repodata observation (`repomd.xml` revision). Populated
by `cadence collect repodata` (WP-05). Idempotency: a new observation is
skipped when an existing row already matches `(repo_id, repomd_revision)`.

| Column | Type | Description |
|---|---|---|
| `id` | INTEGER PK | Auto-incremented |
| `repo_id` | TEXT | Slash-delimited identifier, e.g. `ubi9/9/x86_64/baseos` |
| `observed_at` | TIMESTAMP | When CADENCE polled |
| `repomd_revision` | TEXT | `<revision>` from `repomd.xml` (typically an epoch) |
| `primary_xml_sha256` | TEXT | sha256 advertised in `repomd.xml` for `primary.xml.gz`; verified before parsing |

### `repo_package`

One row per package observed in a `repo_observation`'s `primary.xml.gz`.

| Column | Type | Description |
|---|---|---|
| `id` | INTEGER PK | Auto-incremented |
| `observation_id` | INTEGER FK | References `repo_observation.id` |
| `package_name` | TEXT | RPM name |
| `version` | TEXT | EVR (`{epoch}:{version}-{release}`) |
| `arch` | TEXT | Kernel arch |
| `build_time` | TIMESTAMP | `<time build=…>` from primary.xml (nullable) |
| `file_time` | TIMESTAMP | `<time file=…>` from primary.xml (nullable) |

### `container_image`

Per-image rows for both Red Hat Container Catalog (WP-06, `source='catalog'`)
and Quay (WP-07, `source='quay'`). Idempotency: UPSERT on `image_id`.

| Column | Type | Description |
|---|---|---|
| `image_id` | TEXT PK | Catalog: MongoDB ObjectId. Quay: per-arch manifest digest. |
| `source` | TEXT | `catalog` or `quay` |
| `registry` | TEXT | `registry.access.redhat.com` or `quay.io` |
| `repository` | TEXT | e.g. `ubi9/ubi`, `cilium/cilium` |
| `tier` | TEXT | Per `cadence/targets.py` |
| `tag` | TEXT | The build-specific tag if any, else fallback |
| `digest` | TEXT | sha256 content digest |
| `architecture` | TEXT | Kernel arch (translated from catalog/Quay vocab) |
| `build_date` | TIMESTAMP | Catalog: `brew.completion_date` ∨ `creation_date`. Quay: tag `start_ts` (push time). |
| `parsed_version` | TEXT | First half of an `X[.Y[.Z]]-NNN` tag, else NULL |
| `parsed_build_num` | INTEGER | Second half of the same pattern, else NULL |
| `raw_json` | TEXT | Verbatim upstream image record (or tag listing for Quay) |
| `collected_at` | TIMESTAMP | When CADENCE pulled it |

### `container_image_rpm`

RPM manifest for each catalog image. One row per `(image_id, package_name,
arch)`. **No rows for Quay images in v1.**

| Column | Type | Description |
|---|---|---|
| `image_id` | TEXT FK | References `container_image.image_id` |
| `package_name` | TEXT | RPM name |
| `version` | TEXT | EVR with epoch recovered from `srpm_nevra` |
| `arch` | TEXT | Kernel arch as reported by the catalog manifest |

### `catalog_advisory_mapping`

Legacy `comparison.advisory_rpm_mapping` field captured from pre-November
2024 catalog records. Used as a cross-validation signal in WP-09; never
authoritative.

| Column | Type | Description |
|---|---|---|
| `image_id` | TEXT FK | References `container_image.image_id` |
| `advisory_id` | TEXT | `RHSA-…` or `RHBA-…` |
| `nvra` | TEXT | `name-version-release.arch` per the catalog field |

### `tracked_repository`

Audit table of every repository CADENCE iterates. Seeded from
`cadence/targets.py` on every collector run.

| Column | Type | Description |
|---|---|---|
| `repository` | TEXT PK | e.g. `ubi9/ubi` |
| `source` | TEXT | `catalog` or `quay` |
| `registry` | TEXT | `registry.access.redhat.com` or `quay.io` |
| `tier` | TEXT | Per `cadence/targets.py` |
| `rationale` | TEXT | Why this repo is tracked (selection-bias audit) |
| `added_at` | TIMESTAMP | First time CADENCE saw this repo in `targets.py` |

### `gap_measurement`

Output of `cadence analyze reconstruct` (WP-09). One row per
`(rhsa_id, repository, architecture, package_name, fixed_version,
methodology_version)`. Idempotency: DELETE WHERE methodology_version=? +
INSERT.

| Column | Type | Description |
|---|---|---|
| `id` | INTEGER PK | Auto-incremented |
| `rhsa_id` | TEXT FK | References `rhsa.rhsa_id` |
| `repository` | TEXT | Tracked repo |
| `tier` | TEXT | Per `cadence/targets.py` |
| `architecture` | TEXT | Kernel arch |
| `package_name` | TEXT | RPM name |
| `fixed_version` | TEXT | EVR |
| `rhsa_published_at` | TIMESTAMP | Copied from `rhsa.published_at` for self-contained rows |
| `repo_first_seen_at` | TIMESTAMP | First UBI observation carrying `>= fixed_version` (nullable) |
| `image_first_built_at` | TIMESTAMP | First catalog image carrying the fix (nullable) |
| `image_id` | TEXT FK | That image's id (nullable) |
| `gap_a_seconds` | INTEGER | `repo_first_seen_at - rhsa.published_at` (nullable) |
| `gap_b_seconds` | INTEGER | `image_first_built_at - repo_first_seen_at` (nullable) |
| `gap_c_seconds` | INTEGER | `image_first_built_at - rhsa.published_at` (nullable) |
| `computed_at` | TIMESTAMP | When the reconstruction ran |
| `methodology_version` | TEXT | Tag for this analysis run (default `v1`) |

### `rebuild_interval`

One row per consecutive pair of `container_image` rows within a
`(repository, architecture)` group. Full-table replace on every reconstruction.

| Column | Type | Description |
|---|---|---|
| `id` | INTEGER PK | Auto-incremented |
| `repository` | TEXT | Container repo |
| `tier` | TEXT | Per `cadence/targets.py` |
| `architecture` | TEXT | Kernel arch |
| `prior_image_id` | TEXT FK | Earlier image of the pair |
| `next_image_id` | TEXT FK | Later image |
| `prior_build_date` | TIMESTAMP | `container_image.build_date` of prior |
| `next_build_date` | TIMESTAMP | `container_image.build_date` of next |
| `interval_seconds` | INTEGER | `next - prior` in seconds, non-negative |
| `computed_at` | TIMESTAMP | When the reconstruction ran |

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
