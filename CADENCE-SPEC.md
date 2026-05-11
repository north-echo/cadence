# CADENCE: Container Supply Chain Patch Latency Measurement

**Repository:** `north-echo/cadence`
**PyPI distribution name:** `ne-cadence`
**Python import name:** `cadence`
**CLI binary:** `cadence`
**Owner:** Christopher Lusk <clusk@northecho.dev>
**Identity:** All commits signed `Signed-off-by: Christopher Lusk <clusk@northecho.dev>` and `Assisted-by: Claude (Anthropic)`
**License:** Apache-2.0 (code), CC-BY-4.0 (produced dataset)
**Stack:** Python 3.12+, Click + Rich, SQLite, httpx, rootless Podman, Fedora Server 42
**Target host:** OptiPlex 7060 Micro (Fluxgate Research Station, i7-8700T, 16GB DDR4, Fedora Server 42), cohabitating with Fluxgate

---

## 1. Project Thesis

CADENCE measures **end-to-end patch latency in multi-hop container supply chains** by polling public Red Hat data sources and computing the time between RHSA publication and the appearance of fixed packages in successively-downstream container images.

The container supply chain is multi-hop:

1. **Hop 1:** RHEL fix lands (RHSA published)
2. **Hop 2:** UBI consumes from RHEL → new UBI base image
3. **Hop 3:** Commercial managed service rebuilds against new UBI (OpenShift platform, RHACM, MCE, ODF, Logging, Service Mesh, etc.)
4. **Hop 4:** Quay-hosted community/partner content (operators, ISV images) rebuilds on independent cadences

Each hop has its own rebuild discipline. Cumulative latency is what users actually feel. CADENCE measures each publicly-observable hop independently and characterizes the full distribution per tier.

**Pre-flight findings (already validated against public APIs) that justify the project:**

- UBI rebuild cadence has accelerated materially in recent years (median inter-build interval ~5 days for ubi9/ubi amd64 in last 12 months, vs. ~12 days all-time average)
- OpenShift platform images (`openshift4/ose-*`) track UBI cadence closely (median ~7 days, p90 ~20 days)
- **Layered Red Hat products are the bottleneck:** RHACM, MCE, Service Mesh show median 16-26 days, p90 40-61 days, with individual gaps up to 91 days
- The Container Catalog API exposes full historical data back to UBI launch dates (UBI 8 from 2019, UBI 9 from 2022, UBI 10 from 2025) — substantially more than initially scoped
- The catalog API exposed an `advisory_rpm_mapping` field on images until ~November 2024, then stopped populating it; CADENCE will compute its own mapping and use the legacy field as cross-validation for older images
- `cdn-ubi.redhat.com` only exposes current repodata (no archive); Gap A is forward-only
- Quay.io requires custom collection; v1 measures inter-build interval only (RPM-level Gap C deferred to v2)

---

## 2. Goals & Non-Goals

### Goals

- Quantify three latency gaps for Red Hat container supply chains:
  - **Gap A:** RHSA publication → fixed RPM in `cdn-ubi.redhat.com` (forward-only)
  - **Gap B:** RPM availability → first rebased base image
  - **Gap C:** RHSA publication → first downstream image with fix (end-to-end, per tier)
- Produce a publishable dataset spanning the full available history of UBI 8/9/10, plus representative coverage of OpenShift platform, layered Red Hat products, and Quay-hosted content
- Slice by RHSA severity, package, image variant, product tier, UBI major version, architecture, and time period
- Produce reproducible findings with documented methodology, suitable for blog post, conference talk, or research paper
- Run on Christopher's homelab stack (Fedora Server, rootless Podman, SQLite, Python 3.12+, Click, Rich)

### Non-Goals

- Does not patch images (separate downstream project, design informed by CADENCE findings)
- Does not scan running clusters or production workloads
- Does not evaluate patch quality or correctness, only timing of publication
- Does not require Red Hat subscriptions; everything used is publicly accessible
- Not tied to FedRAMP, ROSA GovCloud, or any compliance regime; FedRAMP is one downstream use case among many, not the framing
- Does not attempt to extract RPM manifests from Quay images in v1 (deferred to v2)

### Methodological constraints

- All findings must be reproducible from public data sources
- Collectors must record raw upstream data (`raw_json` columns) for audit
- No heuristics or inferences in the raw data layer; those belong in analysis with explicit documentation
- Dataset must be releasable (CC-BY-4.0) so external researchers can verify findings
- Published findings must include a threats-to-validity document

---

## 3. Data Sources

| Source | URL Pattern | Auth | Purpose |
|---|---|---|---|
| Red Hat Security Data API | `https://access.redhat.com/hydra/rest/securitydata/cvrf.json` | None | RHSA list, fixed packages, publish timestamps |
| Red Hat CSAF Documents | `https://access.redhat.com/security/data/csaf/v2/advisories/` | None | CSAF/VEX statements per RHSA |
| UBI repodata | `https://cdn-ubi.redhat.com/content/public/ubi/dist/ubi{8,9,10}/{ver}/{arch}/{repo}/os/repodata/` | None | RPM availability timestamps (forward-only) |
| Red Hat Container Catalog API | `https://catalog.redhat.com/api/containers/v1/` | None | Image tags, build dates, RPM manifests, legacy advisory mapping |
| Quay.io API | `https://quay.io/api/v1/repository/{ns}/{name}/...` | None for public repos | Tag history, last_modified timestamps for Quay-hosted images |
| OCI Distribution v2 | `https://quay.io/v2/{name}/...` | None for public repos | Standard registry protocol, manifest creation dates |
| Registry inspection | `skopeo inspect docker://...` | None for public images | Cross-validation of catalog data |

**Polite-polling defaults:**
- 1 request/second per host (configurable)
- Exponential backoff with jitter on 429/5xx
- Response caching with TTL (24h for stable historical data, 1h for current state)
- Persistent on-disk cache in `~/.cache/cadence/`

---

## 4. Data Model

```sql
-- 001_initial.sql

CREATE TABLE rhsa (
    rhsa_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    severity TEXT NOT NULL,
    published_at TIMESTAMP NOT NULL,
    updated_at TIMESTAMP,
    source_url TEXT NOT NULL,
    raw_json TEXT NOT NULL,
    collected_at TIMESTAMP NOT NULL
);

CREATE TABLE rhsa_cve (
    rhsa_id TEXT NOT NULL,
    cve_id TEXT NOT NULL,
    cvss3_score REAL,
    cvss3_vector TEXT,
    PRIMARY KEY (rhsa_id, cve_id),
    FOREIGN KEY (rhsa_id) REFERENCES rhsa(rhsa_id)
);

CREATE TABLE rhsa_package_fix (
    rhsa_id TEXT NOT NULL,
    package_name TEXT NOT NULL,
    fixed_version TEXT NOT NULL,
    arch TEXT NOT NULL,
    product TEXT NOT NULL,
    PRIMARY KEY (rhsa_id, package_name, fixed_version, arch, product),
    FOREIGN KEY (rhsa_id) REFERENCES rhsa(rhsa_id)
);

CREATE TABLE rhsa_vex (
    rhsa_id TEXT NOT NULL,
    product_id TEXT NOT NULL,
    status TEXT NOT NULL,  -- not_affected | affected | fixed | under_investigation
    justification TEXT,
    PRIMARY KEY (rhsa_id, product_id),
    FOREIGN KEY (rhsa_id) REFERENCES rhsa(rhsa_id)
);

CREATE TABLE repo_observation (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_id TEXT NOT NULL,
    observed_at TIMESTAMP NOT NULL,
    repomd_revision TEXT NOT NULL,
    primary_xml_sha256 TEXT NOT NULL
);

CREATE TABLE repo_package (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    observation_id INTEGER NOT NULL,
    package_name TEXT NOT NULL,
    version TEXT NOT NULL,
    arch TEXT NOT NULL,
    build_time TIMESTAMP,
    file_time TIMESTAMP,
    FOREIGN KEY (observation_id) REFERENCES repo_observation(id)
);

CREATE INDEX idx_repo_package_lookup ON repo_package(package_name, version, arch);

-- Catalog images (registry.access.redhat.com)
CREATE TABLE container_image (
    image_id TEXT PRIMARY KEY,
    source TEXT NOT NULL,                 -- 'catalog' | 'quay'
    registry TEXT NOT NULL,
    repository TEXT NOT NULL,
    tier TEXT NOT NULL,                   -- 'ubi' | 'ocp_platform' | 'rh_layered' | 'quay_community' | 'quay_partner' | 'quay_redhat' | 'other'
    tag TEXT NOT NULL,
    digest TEXT NOT NULL,
    architecture TEXT NOT NULL,
    build_date TIMESTAMP NOT NULL,
    parsed_version TEXT,
    parsed_build_num INTEGER,
    raw_json TEXT NOT NULL,
    collected_at TIMESTAMP NOT NULL
);

CREATE INDEX idx_container_image_repo_tag ON container_image(repository, tag);
CREATE INDEX idx_container_image_build ON container_image(repository, build_date);
CREATE INDEX idx_container_image_tier ON container_image(tier, build_date);

CREATE TABLE container_image_rpm (
    image_id TEXT NOT NULL,
    package_name TEXT NOT NULL,
    version TEXT NOT NULL,
    arch TEXT NOT NULL,
    PRIMARY KEY (image_id, package_name, arch),
    FOREIGN KEY (image_id) REFERENCES container_image(image_id)
);

CREATE INDEX idx_image_rpm_lookup ON container_image_rpm(package_name, version);

-- Legacy advisory_rpm_mapping (catalog only, pre-November 2024)
CREATE TABLE catalog_advisory_mapping (
    image_id TEXT NOT NULL,
    advisory_id TEXT NOT NULL,
    nvra TEXT NOT NULL,
    PRIMARY KEY (image_id, advisory_id, nvra),
    FOREIGN KEY (image_id) REFERENCES container_image(image_id)
);

CREATE TABLE gap_measurement (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rhsa_id TEXT NOT NULL,
    repository TEXT NOT NULL,
    tier TEXT NOT NULL,
    architecture TEXT NOT NULL,
    package_name TEXT NOT NULL,
    fixed_version TEXT NOT NULL,
    rhsa_published_at TIMESTAMP NOT NULL,
    repo_first_seen_at TIMESTAMP,
    image_first_built_at TIMESTAMP,
    image_id TEXT,
    gap_a_seconds INTEGER,
    gap_b_seconds INTEGER,
    gap_c_seconds INTEGER,
    computed_at TIMESTAMP NOT NULL,
    methodology_version TEXT NOT NULL,
    FOREIGN KEY (rhsa_id) REFERENCES rhsa(rhsa_id),
    FOREIGN KEY (image_id) REFERENCES container_image(image_id)
);

CREATE INDEX idx_gap_lookup ON gap_measurement(repository, rhsa_id);
CREATE INDEX idx_gap_tier ON gap_measurement(tier, rhsa_published_at);

-- Inter-build interval as a first-class metric
CREATE TABLE rebuild_interval (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repository TEXT NOT NULL,
    tier TEXT NOT NULL,
    architecture TEXT NOT NULL,
    prior_image_id TEXT NOT NULL,
    next_image_id TEXT NOT NULL,
    prior_build_date TIMESTAMP NOT NULL,
    next_build_date TIMESTAMP NOT NULL,
    interval_seconds INTEGER NOT NULL,
    computed_at TIMESTAMP NOT NULL,
    FOREIGN KEY (prior_image_id) REFERENCES container_image(image_id),
    FOREIGN KEY (next_image_id) REFERENCES container_image(image_id)
);

CREATE INDEX idx_rebuild_tier ON rebuild_interval(tier, next_build_date);

-- Configured tracking targets (so we can audit "which repos are we tracking and why")
CREATE TABLE tracked_repository (
    repository TEXT PRIMARY KEY,
    source TEXT NOT NULL,                 -- 'catalog' | 'quay'
    registry TEXT NOT NULL,
    tier TEXT NOT NULL,
    rationale TEXT NOT NULL,              -- why we picked this repo (selection bias documentation)
    added_at TIMESTAMP NOT NULL
);
```

**Key model notes:**
- `raw_json` columns preserve full upstream records for re-analysis without re-collection
- `gap_measurement` and `rebuild_interval` are derived; can be regenerated with new methodology versions
- `tier` field is critical: enables the multi-hop tier slicing that drives the headline finding
- NEVRA comparisons must use `rpm.labelCompare()` from rpm Python bindings, not string comparison
- All timestamps UTC; document this in `data-dictionary.md`

---

## 5. Architecture

```
cadence/
├── pyproject.toml                       # name = "ne-cadence", scripts.cadence = "cadence.cli:main"
├── README.md
├── LICENSE                              # Apache-2.0
├── DATASET-LICENSE                      # CC-BY-4.0
├── Containerfile                        # rootless Podman, Fedora 42 base
├── systemd/
│   └── user/                            # user-level units, installed via systemctl --user
│       ├── cadence-collect-rhsa.{service,timer}
│       ├── cadence-collect-csaf.{service,timer}
│       ├── cadence-collect-repodata.{service,timer}
│       ├── cadence-collect-catalog.{service,timer}
│       └── cadence-collect-quay.{service,timer}
├── cadence/
│   ├── __init__.py
│   ├── cli.py                           # Click entrypoint
│   ├── config.py                        # Pydantic Settings
│   ├── db.py                            # SQLite connection + migration runner
│   ├── targets.py                       # Configured tracking targets per tier
│   ├── collectors/
│   │   ├── __init__.py
│   │   ├── base.py                      # BaseCollector ABC, retry, caching, rate limit
│   │   ├── rhsa.py                      # Red Hat Security Data API
│   │   ├── csaf.py                      # CSAF/VEX documents
│   │   ├── repodata.py                  # cdn-ubi.redhat.com (forward-only)
│   │   ├── catalog.py                   # Red Hat Container Catalog API
│   │   ├── quay.py                      # Quay public API + OCI v2
│   │   └── registry.py                  # skopeo wrapper for verification
│   ├── analysis/
│   │   ├── __init__.py
│   │   ├── reconstruct.py               # Backfill historical state
│   │   ├── gaps.py                      # Compute Gap A, B, C
│   │   ├── intervals.py                 # Inter-build interval distributions
│   │   ├── slice.py                     # Faceted analysis
│   │   └── export.py                    # Dataset publication formats
│   ├── reports/
│   │   ├── __init__.py
│   │   ├── summary.py                   # Rich-formatted CLI output
│   │   ├── markdown.py                  # Markdown report generation
│   │   └── charts.py                    # matplotlib + plotly
│   └── schema/
│       ├── 001_initial.sql
│       ├── 002_csaf.sql                 # if separated
│       └── ... migrations
├── tests/
│   ├── conftest.py
│   ├── fixtures/                        # Recorded API responses for offline tests
│   ├── test_collectors/
│   ├── test_analysis/
│   └── test_reports/
└── docs/
    ├── methodology.md
    ├── data-dictionary.md
    ├── reproducing-findings.md
    ├── threats-to-validity.md
    └── operations.md                    # cohabitation, resource budgets, install/pause
```

**Stack pins:**
- Python 3.12+
- httpx (async-capable HTTP)
- click + rich (CLI)
- pydantic + pydantic-settings (config)
- sqlite3 (stdlib) or sqlite-utils
- structlog (structured logging)
- pytest + pytest-httpx (or vcrpy) for offline tests
- hypothesis for property-based tests
- matplotlib (static) + plotly (interactive) for charts
- rpm Python bindings for NEVRA comparison
- uv for build/dependency management

**CLI surface (top-level):**
```
cadence --help
cadence db init
cadence db migrate
cadence collect rhsa [--since DATE] [--until DATE]
cadence collect csaf [--rhsa RHSA-ID | --all-known]
cadence collect repodata [--repos REPO_ID,...]
cadence collect catalog [--repos REPO,...] [--since DATE]
cadence collect quay [--repos NS/NAME,...]
cadence verify image REPO:TAG
cadence verify random --sample N
cadence analyze reconstruct [--methodology-version VERSION]
cadence analyze gaps [--gap A|B|C] [--slice-by FACET]
cadence analyze intervals [--slice-by FACET]
cadence report summary
cadence report markdown --output FILE.md
cadence report charts --output-dir DIR
cadence export dataset --output-dir DIR
cadence export raw --output-file FILE.tar.zst
cadence health
cadence version
```

---

## 6. Tracked Repository Set (Initial v1 Coverage)

This is the v1 default `targets.py` content. Selection rationale must be recorded in `tracked_repository.rationale`.

### Tier: `ubi` (UBI base images)

| Repository | Registry | Rationale |
|---|---|---|
| ubi8/ubi | registry.access.redhat.com | Reference UBI 8 standard variant |
| ubi8/ubi-minimal | registry.access.redhat.com | UBI 8 minimal, common in slim images |
| ubi8/ubi-micro | registry.access.redhat.com | UBI 8 micro, smallest variant |
| ubi9/ubi | registry.access.redhat.com | Reference UBI 9 standard variant |
| ubi9/ubi-minimal | registry.access.redhat.com | UBI 9 minimal |
| ubi9/ubi-micro | registry.access.redhat.com | UBI 9 micro |
| ubi9/ubi-init | registry.access.redhat.com | UBI 9 with init system |
| ubi10/ubi | registry.access.redhat.com | UBI 10 (launched May 2025) |
| ubi10/ubi-minimal | registry.access.redhat.com | UBI 10 minimal |
| ubi10/ubi-micro | registry.access.redhat.com | UBI 10 micro |

### Tier: `ocp_platform` (OpenShift core platform)

| Repository | Rationale |
|---|---|
| openshift4/ose-cli | Core OpenShift CLI image, broad usage |
| openshift4/ose-installer | OCP installer, spike-validated 1900+ tags |
| openshift4/ose-haproxy-router | Default ingress component |
| openshift4/ose-kube-rbac-proxy | Common platform component |

### Tier: `rh_layered` (Red Hat layered products)

| Repository | Rationale |
|---|---|
| rhacm2/console-rhel9 | RHACM core component |
| multicluster-engine/cluster-curator-controller-rhel9 | MCE core component |
| openshift-logging/cluster-logging-operator-bundle | Logging operator |
| openshift-logging/vector-rhel9 | Logging data plane |
| odf4/odf-rhel9-operator | ODF operator |
| odf4/cephcsi-rhel9 | ODF Ceph CSI |
| openshift-service-mesh/istio-rhel9-operator | Service Mesh operator |

### Tier: `quay_redhat` (Red Hat content on Quay)

| Repository | Rationale |
|---|---|
| redhat/ubi9 | Red Hat publishes UBI to Quay; useful comparison vs. registry.access.redhat.com |
| redhat/ubi9-minimal | Same |

### Tier: `quay_community` (Community operators / projects)

Probe these via the Quay collector. Selection rationale: high-profile projects commonly run on OpenShift.

| Repository | Rationale |
|---|---|
| cilium/cilium | Major CNI option |
| cilium/cilium-operator-generic | Cilium operator |
| argoproj/argocd | Major GitOps tool |
| prometheus/prometheus | Common observability |
| prometheus-operator/prometheus-operator | Common observability operator |
| jaegertracing/jaeger-operator | Tracing |
| kiali/kiali | Service mesh observability |
| strimzi/strimzi-operator | Kafka operator |
| kubevirt/virt-operator | Virtualization |
| projectquay/quay | Quay itself |

### Tier: `quay_partner` (Partner-certified examples)

| Repository | Rationale |
|---|---|
| crunchydata/postgres-operator | Major partner, security-focused |
| bitnami/postgresql | Common Bitnami image |
| bitnami/redis | Common Bitnami image |

**Selection bias:** This list is curated, not exhaustive. The `tracked_repository.rationale` column documents why each repo was added. WP-12 documentation must include a "selection bias" section in `threats-to-validity.md`. Users can extend the tracked set via `cadence collect catalog --repos REPO,...` or `cadence collect quay --repos NS/NAME,...`.

---

## 7. Work Packages

Each work package is sized for one Claude Code session with clear entry/exit criteria. Tests pass before moving to the next.

### WP-01: Project Skeleton & Configuration

**Goal:** Bootstrap the project with directory structure, `pyproject.toml`, CLI skeleton, configuration loading, and SQLite migrations.

**Deliverables:**
- `pyproject.toml`:
  - `name = "ne-cadence"`
  - `[project.scripts] cadence = "cadence.cli:main"`
  - Dependencies: httpx, click, rich, pydantic, pydantic-settings, structlog, pytest, pytest-httpx, hypothesis, matplotlib, plotly
  - rpm Python bindings as optional dep (system-provided on Fedora)
- `cadence/cli.py` with `cadence --help` showing all subcommand stubs from Section 5
- `cadence/config.py` loading from `~/.config/cadence/config.toml` and env vars with `CADENCE_` prefix
- `cadence/db.py` with migration runner applying SQL files from `cadence/schema/` in lexicographic order; default DB location `~/.local/share/cadence/cadence.db`
- `cadence/schema/001_initial.sql` matching Section 4
- `cadence/targets.py` containing the Section 6 tracked repository set as Python data structures
- `Containerfile` for rootless Podman build, Fedora 42 base
- `.gitignore`, `LICENSE` (Apache-2.0), `DATASET-LICENSE` (CC-BY-4.0)
- README skeleton (full README is WP-12)
- Pre-commit hook config requiring DCO sign-off

**Acceptance:**
- `cadence --help` runs and shows all subcommand stubs
- `cadence db init` creates the database with all schema applied
- `cadence db migrate` is idempotent
- `podman build -t cadence .` succeeds rootlessly
- `pytest` runs (zero tests acceptable; harness must work)

### WP-02: Collector Base Infrastructure

**Goal:** Shared collector machinery: HTTP client with rate limiting, retry, caching, base ABC.

**Deliverables:**
- `cadence/collectors/base.py`:
  - `BaseCollector` ABC with abstract `collect()` method
  - Async httpx client with per-host rate limiting (default: 1 req/sec, configurable)
  - Exponential backoff with jitter on 429/5xx
  - Disk-based response caching with configurable TTL (default 24h stable / 1h current)
  - `structlog`-based structured logging
- Global `--cache-dir` CLI option (default `~/.cache/cadence/`)
- Tests using pytest-httpx with recorded fixtures
- Methodology documentation stub in `docs/methodology.md`

**Acceptance:**
- Sample `EchoCollector` subclass fetches a fixture URL, retries on simulated failure, caches the response
- Tests pass offline (no live network calls in CI)
- Rate limit demonstrably throttles bursts

### WP-03: RHSA Collector

**Goal:** Pull all RHSAs affecting RHEL/UBI from the Red Hat Security Data API.

**Endpoint:** `https://access.redhat.com/hydra/rest/securitydata/cvrf.json`

**Deliverables:**
- `cadence/collectors/rhsa.py` implementing `RHSACollector(BaseCollector)`
- Pagination handling
- Filter to RHSAs affecting RHEL 8/9/10 packages
- Persistence to `rhsa`, `rhsa_cve`, `rhsa_package_fix`
- CLI: `cadence collect rhsa --since YYYY-MM-DD [--until YYYY-MM-DD]`
- Idempotent re-runs; updates rows if upstream record changed
- Captures both publish and update timestamps

**Acceptance:**
- `cadence collect rhsa --since 2025-01-01 --until 2025-01-31` populates RHSA tables
- Re-running the same range does not duplicate rows
- Test fixtures cover at least one Critical, one Important, one Moderate, one Low, and one multi-CVE RHSA
- Edge cases: RHSAs with no fixed packages, multi-product RHSAs, multi-CVE RHSAs

### WP-04: CSAF/VEX Collector

**Goal:** Pull CSAF documents to enrich RHSA records with VEX statements.

**Endpoint:** `https://access.redhat.com/security/data/csaf/v2/advisories/{rhsa-lower}.json`

**Deliverables:**
- `cadence/collectors/csaf.py` implementing `CSAFCollector(BaseCollector)`
- Per-RHSA fetch
- Persistence to `rhsa_vex`
- CLI: `cadence collect csaf [--rhsa RHSA-ID | --all-known]`
- Migration `002_csaf.sql` if separating from initial migration

**Acceptance:**
- VEX statements correctly extracted for representative RHSAs
- Test fixtures cover all four VEX status values
- Handles missing CSAF documents gracefully

### WP-05: UBI Repodata Collector (Forward-Only)

**Goal:** Poll UBI repository metadata to detect when fixed RPMs become available. **Forward-only** — no historical reconstruction possible (validated in pre-flight spike: cdn-ubi exposes only current state).

**Endpoints:**
- `https://cdn-ubi.redhat.com/content/public/ubi/dist/ubi{8,9,10}/{ver}/{arch}/{baseos|appstream|codeready-builder}/os/repodata/repomd.xml`
- ...and the corresponding `primary.xml.gz`

**Deliverables:**
- `cadence/collectors/repodata.py` implementing `RepoDataCollector(BaseCollector)`
- Repo set in config: UBI 8/9/10 baseos + appstream + codeready-builder, x86_64 + aarch64
- Parse `repomd.xml` for current revision and `primary.xml.gz` location
- Parse `primary.xml.gz` for package list with `build_time` and `file_time`
- Persistence to `repo_observation` and `repo_package`
- Idempotent: skip re-parsing if `repomd_revision` unchanged
- CLI: `cadence collect repodata [--repos REPO_ID,...]`
- `methodology.md` MUST document Gap A as forward-only with polling-interval-bounded precision

**Acceptance:**
- Initial collection populates package data for all configured repos
- Subsequent runs detect new package versions
- XML namespace handling correct
- Note in `methodology.md`: UBI `updateinfo.xml` is empty (validated in spike); CADENCE does not consume it

### WP-06: Container Catalog Collector

**Goal:** Pull all images and RPM manifests for the Section 6 catalog-source repos. Backfill spans **full available history** per UBI version (UBI 8: 2019, UBI 9: 2022, UBI 10: 2025).

**Endpoints:**
- Repo metadata: `https://catalog.redhat.com/api/containers/v1/repositories?filter=repository=={repo}`
- Images: `https://catalog.redhat.com/api/containers/v1/repositories/registry/registry.access.redhat.com/repository/{repo}/images?page_size=100&page=N&sort_by=creation_date%5Basc%5D&filter=architecture=={arch}`
- RPM manifest: `https://catalog.redhat.com/api/containers/v1/images/id/{image_id}/rpm-manifest`

**Deliverables:**
- `cadence/collectors/catalog.py` implementing `CatalogCollector(BaseCollector)`
- Iterates `(repository, architecture)` pairs from `tracked_repository`
- For each image: fetch RPM manifest from `_links.rpm_manifest.href`
- Captures `repositories[].comparison.advisory_rpm_mapping` when populated (pre-November 2024) into `catalog_advisory_mapping`
- Persists to `container_image` and `container_image_rpm`, sets `tier` and `source='catalog'`
- Parses tag into `parsed_version`/`parsed_build_num` (e.g., `9.5-1736` → `('9.5', 1736)`)
- CLI:
  - `cadence collect catalog` — collect all configured catalog repos
  - `cadence collect catalog --repos REPO,...` — collect specific repos
  - `cadence collect catalog --since YYYY-MM-DD` — incremental
- Defaults to **full historical backfill** on first run (per spike findings)

**Acceptance:**
- All Section 6 catalog-source repos populated
- RPM manifests linked to parent images
- `catalog_advisory_mapping` populated for older images, empty for post-November 2024 images
- Tags not matching expected pattern logged but not fatal
- Test fixtures cover catalog API pagination
- Backfill of full UBI history completes in approx. 3 hours at default rate limits

### WP-07: Quay Collector

**Goal:** Pull tag history and inter-build interval data for Section 6 Quay-source repos.

**Endpoints:**
- Repo metadata: `https://quay.io/api/v1/repository/{ns}/{name}` (no auth for public repos)
- Tags: `https://quay.io/api/v1/repository/{ns}/{name}/tag/?limit=100&page=N&onlyActiveTags=true`
- OCI v2 manifest: `https://quay.io/v2/{ns}/{name}/manifests/{ref}` with `Accept: application/vnd.oci.image.index.v1+json,application/vnd.oci.image.manifest.v1+json,application/vnd.docker.distribution.manifest.v2+json,application/vnd.docker.distribution.manifest.list.v2+json`

**Deliverables:**
- `cadence/collectors/quay.py` implementing `QuayCollector(BaseCollector)`
- For each tracked Quay repo: pull all active tags with `start_ts` and `last_modified`
- For each unique manifest digest: pull manifest, extract `created` timestamp (and per-arch manifests if a manifest list)
- Persists to `container_image` with `source='quay'` and appropriate tier
- **No RPM manifest extraction in v1** (deferred to v2; document this in `methodology.md`)
- CLI:
  - `cadence collect quay` — all configured Quay repos
  - `cadence collect quay --repos NS/NAME,...` — specific repos
- Selection bias documentation: `tracked_repository.rationale` populated for every Quay repo

**Acceptance:**
- All Section 6 Quay-source repos populated
- Manifest list resolution correct (per-arch entries)
- `container_image` rows have `source='quay'`, populated `tier`, `digest`, `architecture`, `build_date`
- Test fixtures cover Quay API pagination and OCI manifest list resolution
- Methodology documents Quay's lack of RPM manifest as a v1 limitation

### WP-08: Registry Verification (skopeo wrapper)

**Goal:** Cross-validate catalog/Quay data against actual registry state.

**Deliverables:**
- `cadence/collectors/registry.py` wrapping `skopeo inspect`
- `cadence verify image REPO:TAG` compares registry digest, build date, labels against database
- `cadence verify random --sample N` picks N random images and verifies
- Reports discrepancies but does not fail the dataset
- Soft dependency on `skopeo` binary; graceful degradation if absent

**Acceptance:**
- Verification of a known-good image succeeds
- Detects intentionally-corrupted database state
- `methodology.md` documents that catalog API is authoritative when discrepancies found

### WP-09: Backfill Reconstruction

**Goal:** Compute Gap C and inter-build intervals from collected raw data.

**Deliverables:**
- `cadence/analysis/reconstruct.py`:
  - NEVRA comparison via `rpm.labelCompare()`
  - For each `(rhsa_id, package_name, repository)`: find earliest `repo_package` where version ≥ fixed
  - For each `(rhsa_id, package_name, repository)`: find earliest `container_image` (by `build_date`) where `container_image_rpm` has version ≥ fixed
  - Populates `gap_measurement` with all three gaps and tier
- `cadence/analysis/intervals.py`:
  - For each `(repository, architecture)`: compute consecutive-image intervals
  - Populates `rebuild_interval` table
  - One row per consecutive pair
- Methodology versioning via `methodology_version` column; bumping it allows parallel re-analysis
- Cross-validation pass: where `catalog_advisory_mapping` exists, validate computed RHSA→image mapping matches recorded mapping; log discrepancies
- Edge case handling documented:
  - RHSAs whose fix never observed in our data → gap recorded as NULL
  - RHSAs published before our earliest observation → gap NULL
  - Per-architecture computation
  - `rhsa_vex` `not_affected` excluded from gap measurement, logged separately
  - Quay images: `gap_measurement` rows have NULL `gap_a/b/c` (no RPM manifest in v1); `rebuild_interval` populated normally
- CLI: `cadence analyze reconstruct [--methodology-version VERSION]`

**Acceptance:**
- Synthetic test data produces correct gap measurements
- NEVRA comparison handles epoch/version/release correctly (property-based tests via Hypothesis)
- Idempotent within a methodology version; new version produces parallel results
- Cross-validation against `catalog_advisory_mapping` reports zero or low discrepancy rate (target: >95% match)
- Limitations documented in `methodology.md`

### WP-10: Gap & Interval Analysis

**Goal:** Compute distributions and faceted slices.

**Deliverables:**
- `cadence/analysis/gaps.py`: distributions of Gap A, B, C
- `cadence/analysis/intervals.py`: distributions of inter-build intervals
- `cadence/analysis/slice.py` slicing by:
  - Tier (the headline facet)
  - RHSA severity
  - Image variant
  - UBI major version
  - Architecture
  - Top-N most-frequently-patched packages
  - Monthly bucketing
  - Day-of-week / day-of-month
- Returns: median, p25, p75, p90, p95, p99, mean, stddev, count
- CLI:
  - `cadence analyze gaps [--gap A|B|C] [--slice-by FACET]`
  - `cadence analyze intervals [--slice-by FACET]`
- Output: Rich table, JSON, CSV
- Warns when N<30 for percentile calculation

**Acceptance:**
- Test dataset with known statistics produces correct percentiles
- Slicing matches manually-verified subsets
- JSON/CSV schema documented in `data-dictionary.md`
- Headline tier comparison reproduces the spike's qualitative finding (UBI ≈ ocp_platform fast; rh_layered slower; quay_community variable)

### WP-11: Reports & Charts

**Goal:** Human-readable summaries and publication-ready charts.

**Deliverables:**
- `cadence report summary` — Rich CLI report covering all gaps, intervals, and major slices
- `cadence report markdown --output FILE.md` — comprehensive Markdown report with chart references
- `cadence report charts --output-dir DIR` — generates PNG (300 DPI) and HTML:
  - **Headline:** box plot of Gap C by tier (UBI, ocp_platform, rh_layered, quay_community, quay_partner)
  - Histogram of Gap C overall
  - CDF of Gap A, B, C overlaid
  - Box plot of inter-build interval by tier
  - Box plot of Gap C by RHSA severity
  - Time-series of monthly inter-build interval median by tier (the "rebuild cadence accelerated" finding)
  - Heatmap of Gap C by package × month (top 20 packages)
  - Box plot of Gap C by architecture
- Colorblind-safe palette (viridis or Okabe-Ito)
- Markdown report is self-contained and renders cleanly on GitHub

**Acceptance:**
- Reports run successfully on a populated database
- Charts have legends, axis labels, titles
- Markdown renders correctly on GitHub
- Headline chart visually communicates the multi-tier finding clearly

### WP-12: Dataset Export

**Goal:** Publishable, reproducible dataset.

**Deliverables:**
- `cadence export dataset --output-dir DIR` produces:
  - `cadence-dataset.parquet`
  - `cadence-dataset.csv`
  - `cadence-dataset.json` (JSON-Lines)
  - `manifest.json` (version, methodology version, time range, row counts, schema, source provenance)
  - `methodology.md` (copy)
  - `LICENSE` (CC-BY-4.0)
- `cadence export raw --output-file FILE.tar.zst`:
  - All `raw_json` columns for full reproducibility
  - Deterministic ordering, no embedded build timestamps beyond the data itself
- No `--anonymize` flag (public data, no anonymization needed)

**Acceptance:**
- Dataset loadable by pandas, polars, jq
- Manifest documents provenance enough for external verification
- Raw archive is reproducible-builds-friendly

### WP-13: Documentation

**Goal:** Documentation sufficient for independent reproduction and external use.

**Deliverables:**

`README.md`:
- Project description, motivation, multi-hop framing
- Installation (pip install ne-cadence; Containerfile)
- Quickstart (collect, analyze, report)
- Architecture overview
- Citation block (BibTeX)

`docs/methodology.md`:
- Data sources with URLs and access notes
- Polling cadence and rate limit policy
- Gap definitions (A, B, C) with worked examples
- Inter-build interval definition
- Tier definitions and selection rationale
- NEVRA comparison approach
- Edge cases and how they're handled
- Known limitations:
  - Gap A is forward-only (cdn-ubi has no archive)
  - Quay v1 has no RPM-level Gap C (deferred to v2)
  - Polling interval bounds Gap A precision

`docs/data-dictionary.md`:
- Every table and column documented
- Every field in exported datasets documented
- Units (seconds for gaps; days conventional in reports) and timezones (UTC throughout)

`docs/reproducing-findings.md`:
- Step-by-step reproduction of a published finding
- Expected runtime and resource consumption
- Sources of variance between runs

`docs/threats-to-validity.md` (this is the credibility document):
- **Selection bias:** which repos are tracked and why; who/what is excluded
- **Observation bias:** Gap A floored by polling interval; document this explicitly
- **Survivorship bias:** deleted/unpublished image tags
- **Catalog ingestion lag:** hypothesized; quantify if possible via skopeo cross-check
- **Pre-November-2024 advisory_rpm_mapping deprecation:** document, use as cross-validation only
- **UBI updateinfo emptiness:** documented; CADENCE does not consume it
- **Quay RPM-manifest absence:** v1 limitation, deferred
- Honest discussion of what conclusions the data does and does not support

**Acceptance:**
- Independent reader can install, run, and reproduce a finding from documentation alone
- Methodology document rigorous enough for academic citation
- Threats-to-validity is honest, specific, and not perfunctory

### WP-14: Continuous Operation & Cohabitation

**Goal:** Long-running collection service that cohabitates cleanly with Fluxgate on the OptiPlex 7060 Micro (i7-8700T, 16GB DDR4, Fedora Server 42).

**Resource budget (target ceilings, document in `docs/operations.md`):**
- Disk: 5GB hard cap on `~/.local/share/cadence/` (database + raw archives). Collection should warn at 4GB and refuse to start at 5GB until the operator runs `cadence db vacuum` or moves the archive.
- Memory: 1GB working set ceiling. Analysis runs that would exceed this should chunk via SQLite cursors, not load full result sets into memory.
- CPU: collection is HTTP-bound; analysis is bursty but bounded to single-digit minutes for full re-analysis runs.
- Network: outbound only, polite-rate-limited per Section 3.

**Cohabitation rules (Fluxgate is the existing tenant):**
- All CADENCE data lives under `~/.local/share/cadence/` (database) and `~/.cache/cadence/` (HTTP cache). Never `/var/lib/...`, never anywhere a system service might collide.
- Run as a non-root user (the operator's own account, same as Fluxgate). No system service unit; user-level systemd timers only.
- Rootless Podman if containerized; bind-mount the data directories from the host user account.
- Timer offsets: every CADENCE timer must specify a minute offset that does not collide with any Fluxgate timer. Default offsets are :17 and :47 past the hour (chosen as unlikely to collide with cron-style :00/:15/:30/:45 conventions). Add `RandomizedDelaySec=10min` to absorb any residual overlap.
- The collection schedule below assumes Fluxgate is the only other periodic workload on the host. If that changes, the operator updates the offsets manually.

**Deliverables:**
- `systemd/user/cadence-collect-rhsa.service` + `cadence-collect-rhsa.timer`
- `systemd/user/cadence-collect-csaf.service` + `cadence-collect-csaf.timer`
- `systemd/user/cadence-collect-repodata.service` + `cadence-collect-repodata.timer`
- `systemd/user/cadence-collect-catalog.service` + `cadence-collect-catalog.timer`
- `systemd/user/cadence-collect-quay.service` + `cadence-collect-quay.timer`
- All units are user-level (`systemd --user`), not system-level.
- Default schedule (each timer uses `OnCalendar=` with `:17` or `:47` minute offset, plus `RandomizedDelaySec=10min`, plus `Persistent=true`):
  - RHSA: every 4 hours, at `02:17, 06:17, 10:17, 14:17, 18:17, 22:17`
  - CSAF: every 4 hours, at `02:47, 06:47, 10:47, 14:47, 18:47, 22:47` (offset 30min after RHSA so CSAF runs against fresh RHSA data)
  - Repodata: every 4 hours, at `00:17, 04:17, 08:17, 12:17, 16:17, 20:17`
  - Catalog: every 12 hours, at `03:47, 15:47` (incremental; full backfill is a manual `cadence collect catalog --since ...` invocation, not timer-driven)
  - Quay: every 12 hours, at `09:17, 21:17`
- `cadence health` command reports last-successful-collection per source, with a non-zero exit code if any source has been silent for more than 2× its expected interval.
- Optional Prometheus metrics endpoint behind `--metrics` flag: collection durations, record counts, error rates, last-success-per-source. Bind to `127.0.0.1` only, never a public interface.
- `docs/operations.md` covers:
  - Installation steps (`systemctl --user enable --now cadence-collect-*.timer`)
  - Cohabitation expectations (Fluxgate alongside; how to add more tenants later by adjusting offsets)
  - Resource budget and what to do when ceilings are hit
  - Backup strategy: nightly `sqlite3 .backup` snapshot to a separate path; weekly archive of raw exports
  - How to safely pause CADENCE during heavy Fluxgate runs (`systemctl --user stop cadence-collect-*.timer`)

**Acceptance:**
- All five user-level timer units install via `systemctl --user enable --now ...` without warnings
- `systemctl --user list-timers` shows all five with non-overlapping next-elapse times
- `cadence health` accurately reports collection state and exits non-zero when a source is stale
- Simulated 24-hour run on the OptiPlex (or equivalent test host) completes without errors and stays under the documented resource ceilings
- `docs/operations.md` exists and is sufficient for an operator to install, monitor, and pause CADENCE without consulting Christopher

### WP-15 (Operational, not Claude Code): First Findings Run

Listed for completeness — this is operational work after WP-01 through WP-14 ship.

- 12+ months of historical data collected (UBI 8/9/10, OCP platform, RH layered)
- 60+ days of forward observation completed
- First findings report generated
- Dataset published (Zenodo for DOI)
- Blog post drafted for `northecho.dev`
- KubeCon EU 2027 talk submitted (or DevConf US / FOSDEM as fallback)

---

## 8. Testing Strategy

- **Unit tests:** every collector, every analysis function, every report generator. Recorded HTTP fixtures (pytest-httpx or vcrpy).
- **Integration tests:** end-to-end pipeline against committed small recorded dataset. <30 seconds in CI.
- **Property-based tests:** Hypothesis for NEVRA comparison and percentile calculation.
- **Methodology tests:** known-input-known-output tests for `reconstruct.py` covering all documented edge cases.
- **CI:** GitHub Actions running `pytest`, `ruff`, `mypy --strict`. No live network in CI.

---

## 9. Out of Scope (Explicit)

- **Patching images.** Separate downstream tool, design informed by CADENCE findings.
- **Other distributions in v1.** Methodology generalizes to Debian, Alpine, Wolfi, Rocky, Alma — but v1 is Red Hat ecosystem only. Multi-distro is v2.
- **Real-time alerting.** Research infrastructure, not a SOC tool.
- **Web UI.** CLI + reports + charts is sufficient.
- **Authentication / multi-tenancy.** Single-user, single-machine.
- **Embedding scanner data (Trivy, Grype).** RHSA + repo + catalog are authoritative; scanners introduce their own latency that would muddy the signal.
- **Quay RPM-level Gap C.** Inter-build interval only in v1; per-image RPM extraction deferred.
- **FedRAMP-specific framing.** FedRAMP downstream is one possible hop-4+ use case among many. Not the framing.

---

## 10. Risks & Mitigations

| Risk | Mitigation |
|---|---|
| Red Hat changes API endpoints/formats | Collector base isolates HTTP; `raw_json` preserves originals for re-parsing |
| Catalog API rate limits trip during backfill | Configurable rate limits, exponential backoff, persistent cache |
| RPM manifests unavailable for older images | Document gap, exclude from analysis, note in threats-to-validity |
| Polling interval bounds Gap A precision | Document explicitly; report as "Gap A ≤ X" when relevant |
| skopeo unavailable | Soft dependency; verification mode degrades gracefully |
| Quay API rate limits or tier listing requirements | Per-repo unauthenticated access works; no namespace enumeration needed |
| Manifest list (multi-arch) edge cases | Test fixtures cover both manifest and manifest-list responses |
| Day-job overlap concerns | Public data only; `clusk@northecho.dev` identity throughout; no Red Hat internal systems |

---

## 11. Identity & Disclosure

- Every commit: `Signed-off-by: Christopher Lusk <clusk@northecho.dev>` and `Assisted-by: Claude (Anthropic)`
- Repo: `north-echo/cadence`, public from day one
- License: Apache-2.0 (code), CC-BY-4.0 (dataset)
- No mention of Red Hat employment, ROSA GovCloud, FedRAMP, or any internal context in the repo
- All findings published under North Echo identity

---

## 12. Definition of Done (v1.0)

- WP-01 through WP-14 complete with passing tests
- Full historical data collected for UBI 8/9/10 and OCP platform tier
- 90+ days of forward observation completed across all tiers
- Dataset published with DOI
- Findings report published as North Echo blog post
- Conference talk submitted to KubeCon EU 2027 (or DevConf US / FOSDEM as fallback)
- README adoption signal (>50 stars, or external citation, or both)

---

## 13. Pre-Validated Findings (Already Confirmed in Spike)

These are findings the eventual report should confirm or refine. Recorded here so v1 reproduces them:

1. **UBI rebuild cadence accelerated 2024-2026.** ubi9/ubi amd64 inter-build interval median: 12 days (all-time) → 5 days (last 12 months). p90: 35 → 19 days.

2. **OpenShift platform tracks UBI cadence closely.** ose-cli, ose-installer, ose-haproxy-router, ose-kube-rbac-proxy all show last-12-month medians of 7 days, p90 of 20 days. Hops 2 and 3 are essentially the same hop for the OpenShift core.

3. **Layered Red Hat products are the bottleneck.** RHACM, MCE, Service Mesh: median 16-26 days, p90 40-61 days, max gaps 76-91 days. The user-felt latency of running these layered products lives here, not in UBI.

4. **The catalog API stopped populating `advisory_rpm_mapping` around November 2024.** Pre-Nov-2024 images have it; post-Nov-2024 images don't. CADENCE computes its own mapping and uses the legacy field as cross-validation only.

5. **`cdn-ubi.redhat.com` exposes only current repodata.** No archive. Gap A is forward-only.

6. **UBI `updateinfo.xml` is empty.** UBI repos do not ship the RHSA→package metadata that entitled RHEL repos do. CADENCE does not consume it.

7. **Catalog API exposes full historical UBI data.** UBI 8 from May 2019, UBI 9 from May 2022, UBI 10 from May 2025. Backfill is tractable (~3 hours at polite rates).

---

## 14. Hand-Off Notes for Claude Code

- Start with WP-01. Do not skip ahead. Each WP's tests must pass before proceeding to the next.
- Check `available_skills` for relevant SKILL.md files before writing code that creates files (especially Python project structure).
- Use `uv` for dependency management; modern Python tooling.
- Commit cadence: one commit per work package minimum, intermediate commits welcome. DCO sign-off is non-negotiable.
- When ambiguity arises between this spec and discovered reality (e.g., API behavior changed since spec was written), prefer reality and document the discrepancy in a NOTES.md or commit message.
- Do not create Red Hat-specific assumptions outside the documented data sources. CADENCE is positioned as general-purpose container ecosystem research that happens to start with Red Hat.
- The threats-to-validity document (WP-13) is load-bearing for credibility. Do not write it perfunctorily.
