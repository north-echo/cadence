# CADENCE Methodology

This document is the canonical reference for anyone reproducing CADENCE
findings. It describes the data CADENCE collects, the choices it makes
when computing patch-latency gaps and inter-build intervals, and the
exact provenance of every number in the published dataset. Cite this
file (and the dataset's `manifest.json`) when citing CADENCE in
research.

**All timestamps are UTC.** Stored values are ISO 8601 strings with
explicit offsets. Reported values are in seconds in the database;
the report and chart layers convert to days for display.

## 1. Data sources

All sources are public and require no authentication. The collectors
record verbatim upstream payloads in `raw_json` columns so any derived
metric can be recomputed without re-fetching.

| Source | URL | Auth | Used for |
|---|---|---|---|
| Red Hat Security Data API | `https://access.redhat.com/hydra/rest/securitydata/csaf.json` | None | RHSA list + per-advisory CSAF v2 documents (RHSA metadata, CVE links, fixed packages, VEX statements) |
| `cdn-ubi.redhat.com` | `https://cdn-ubi.redhat.com/content/public/ubi/dist/ubi{8,9,10}/{ver}/{arch}/{baseos|appstream|codeready-builder}/os/repodata/` | None | UBI repodata (current state only — no archive) |
| Red Hat Container Catalog API | `https://catalog.redhat.com/api/containers/v1/` | None | Image metadata, RPM manifests, legacy advisory_rpm_mapping for pre-Nov-2024 records |
| Quay.io API | `https://quay.io/api/v1/repository/{ns}/{name}/...` | None | Tag history (start_ts, is_manifest_list) |
| OCI Distribution v2 | `https://quay.io/v2/{ns}/{name}/manifests/{ref}` and `/blobs/{digest}` | None | Multi-arch manifest resolution + config-blob arch extraction for single-manifest tags |
| Registry inspection | `skopeo inspect docker://…` | None | Cross-validation (WP-08) |

**Spec discrepancy.** CADENCE-SPEC.md §3 references the retired
`/cvrf.json` endpoint; the live one is `/csaf.json`. The data model
absorbed this transparently; the only difference is the parser. See
[`NOTES.md`](../NOTES.md).

## 2. Polling cadence

CADENCE polls upstreams at the cadence configured for the relevant systemd
timer (see `CADENCE-SPEC.md` §WP-14). Default cadence is documented per
collector; the user-level systemd timers are the authoritative schedule on a
production host.

| Source | Default cadence |
|---|---|
| Red Hat Security Data API (RHSA) | 4 hours |
| Red Hat CSAF | 4 hours, offset 30 min after RHSA |
| `cdn-ubi.redhat.com` repodata | 4 hours |
| Red Hat Container Catalog | 12 hours (incremental) |
| Quay.io | 12 hours |

## 3. Rate limit & retry policy

Every collector goes through the shared `HTTPClient` in
`cadence/collectors/base.py`. The policy is:

* **Per-host rate limit.** Configurable; default is one request per second per
  upstream host. Different hosts do not block one another.
* **Retry on 429 and 5xx.** Up to five retries by default. Backoff is
  exponential with full jitter, capped at 60 seconds.
* **`Retry-After` is honored** when present on a 429 response.
* **Cache before request.** A persistent on-disk cache keyed by
  `sha256(METHOD:URL)` short-circuits identical requests until the entry
  expires.

## 4. Cache policy

The on-disk cache lives in `$XDG_CACHE_HOME/cadence` (default
`~/.cache/cadence`). Entries are JSON with the body base64-encoded so they are
inspectable with standard tools (`jq`, `python -m json.tool`). The TTL is
per-request:

* **Stable historical data:** 24 hours (default).
* **Current-state polling:** 1 hour (default).

Both TTLs are configurable via `CADENCE_CACHE_TTL_STABLE_SECONDS` and
`CADENCE_CACHE_TTL_CURRENT_SECONDS`. Pass `bypass_cache=True` to
`HTTPClient.get` to skip the cache for a single request (used by
cross-validation passes that must hit the upstream).

## 5. NEVRA comparison

Package version comparisons use `rpm.labelCompare()` from the system
`python3-rpm` bindings — never string comparison. This handles epoch,
version, and release components correctly per RPM semantics, including
`~` pre-release markers and `^` post-release markers.

On dev hosts where `python3-rpm` isn't available, `cadence/analysis/nevra.py`
falls back to a pure-Python implementation of the same algorithm
(`rpmvercmp`). Hypothesis property tests
(`tests/test_analysis/test_nevra.py`) check reflexivity and antisymmetry
across 1000+ generated NEVRA pairs, plus a canonical set of known-case
goldens. When both implementations are available, production prefers the
C one.

Stored format: `version` columns hold `{epoch}:{version}-{release}` (the
EVR portion of NEVRA); `arch` is stored separately. `parse_evr()` /
`evr_ge()` in `cadence.analysis.nevra` are the canonical helpers for
comparing the stored strings.

## 6. Gap definitions

CADENCE measures three gaps per `(rhsa, repository, architecture, package,
fixed_version)`. All are recorded in seconds in `gap_measurement`. Any may
be `NULL` when the right-edge anchor wasn't observed.

* **Gap A** = `repo_first_seen_at - rhsa.published_at`
  — RHSA publication to fixed RPM available in `cdn-ubi.redhat.com`.
  **Forward-only.** Pre-flight spike (CADENCE-SPEC.md §13.5) confirmed
  `cdn-ubi.redhat.com` exposes only current repodata, with no archive. Gap A
  precision is bounded by the repodata polling interval: with WP-14's
  default 4-hour timer, Gap A is accurate to ±4 hours, and any reported
  value should be read as "no later than X" rather than "exactly X".
  RPM `build_time` and `file_time` from `<package><time .../></package>`
  in `primary.xml` give a tighter lower bound on availability when present.

* **Gap B** = `image_first_built_at - repo_first_seen_at`
  — fixed RPM available to first rebased base image.

* **Gap C** = `image_first_built_at - rhsa.published_at`
  — end-to-end, RHSA publication to first downstream image containing the
  fix (always computed per tier).

**Worked example.** Suppose `RHSA-2025:0850` is published at 2025-01-30
18:06 UTC and ships `python3-jinja2-0:2.11.3-6.el9_4.noarch`. The next
UBI 9 baseos repodata poll at 2025-01-30 22:17 UTC observes the new RPM
in `cdn-ubi`. A new `ubi9/ubi:9.4-1736` image is then built at 2025-02-01
03:11 UTC, carrying the fixed `python3-jinja2` in its RPM manifest.
CADENCE records `gap_a_seconds = 14_460` (~4 hours, bounded by polling),
`gap_b_seconds = 104_640` (~29 hours), `gap_c_seconds = 119_100`
(~33 hours).

The repodata collector (WP-05) does not consume `updateinfo.xml`: UBI
repositories publish an empty `updateinfo.xml` (validated in the pre-flight
spike, CADENCE-SPEC §13.6). RHSA→package mapping comes from the
`rhsa_package_fix` table (WP-03) joined to `repo_package` by NEVRA.

### Methodology versioning

Every `gap_measurement` row carries a `methodology_version` tag (default
`v1`). The reconstruction pipeline (`cadence analyze reconstruct
[--methodology-version VERSION]`) deletes only rows matching the requested
version before re-inserting; multiple versions coexist in the table.
This lets us re-analyse historical data with new methodology choices
without overwriting the prior dataset — important for reproducibility
of any published finding.

## 7. Inter-build interval

For each `(repository, architecture)` we sort observed `container_image`
rows by `build_date` and record the gap between consecutive builds as a
`rebuild_interval` row. Each row carries
`prior_image_id`/`next_image_id` so the original images are recoverable.
Single-image groups produce no intervals. Quay images participate
normally (the WP-07 caveat about Gap C absence does not apply here).
WP-10 reports median, p25/p75, p90/p95/p99, and mean across slices.

Interval reconstruction is mechanical (no methodology choices), so a
re-run replaces the table wholesale instead of carrying a version tag.

## 8. Tier definitions and selection rationale

A *tier* groups repositories that share a rebuild discipline. It is the
primary slicing dimension behind the headline finding. The seven tiers
are defined in `cadence/targets.py`:

| Tier | Examples | Why it's its own tier |
|---|---|---|
| `ubi` | `ubi8/ubi`, `ubi9/ubi-minimal`, `ubi10/ubi-micro` | Base images; UBI is the upstream all the other RHEL-flavoured tiers depend on |
| `ocp_platform` | `openshift4/ose-cli`, `ose-installer`, `ose-haproxy-router`, `ose-kube-rbac-proxy` | Core OpenShift platform; tracks UBI closely |
| `rh_layered` | `rhacm2/console-rhel9`, `multicluster-engine/*`, `openshift-logging/*`, `odf4/*`, `openshift-service-mesh/*` | Red Hat layered products; the latency bottleneck |
| `quay_redhat` | `redhat/ubi9`, `redhat/ubi9-minimal` | Red Hat content published to Quay; useful comparison vs. `registry.access.redhat.com` |
| `quay_community` | `cilium/cilium`, `argoproj/argocd`, `prometheus/*`, `strimzi/*`, `kubevirt/virt-operator`, `projectquay/quay`, … | Major community projects commonly run on OpenShift |
| `quay_partner` | `crunchydata/postgres-operator`, `bitnami/postgresql`, `bitnami/redis` | Certified-partner content; security-focused examples |
| `other` | (none in v1) | Reserved for repos added at runtime |

The per-repo selection rationale is preserved in the database
(`tracked_repository.rationale`), so the dataset's selection bias is
auditable from the database alone without consulting this document or
the spec.

See [`threats-to-validity.md`](threats-to-validity.md) for an honest
discussion of why this list is what it is and what it excludes.

## 9. Edge cases

The reconstruction pipeline (`cadence/analysis/reconstruct.py`) emits a
`gap_measurement` row for every `(RHSA fix, tracked repo, arch)` triple,
even when the right-edge anchor wasn't observed — the row's existence
documents an unobserved fix. The specific edge cases:

* **RHSAs whose fix never appears in our data.** Gap A and Gap C are
  `NULL`; the row records that we know about the fix but never saw it
  land. Most commonly because of forward-only polling (see §6).
* **RHSAs published before our earliest observation.** Same as above —
  the cdn-ubi snapshot CADENCE first polled may already contain the
  fix, so the earliest `repo_observation` we have is *after* the RHSA
  publication, with no way to know when between RHSA and our first
  poll the RPM actually landed. Gap A is recorded as `NULL`.
* **Per-architecture computation.** Each `(rhsa, repo, package, arch)`
  yields its own row. `noarch` fixes match every container arch;
  `src` entries are skipped (sources don't ship in containers).
* **VEX `not_affected` exclusion.** When `rhsa_vex.status =
  'not_affected'` covers a `(rhsa_id, product)`, that fix is skipped at
  the reconstruction stage and counted in
  `ReconstructResult.not_affected_skipped`.
* **Quay images.** Tracked Quay repositories have no
  `container_image_rpm` rows (WP-07 v1 limitation, §11). Their
  `gap_measurement` rows carry `image_id = NULL` and
  `gap_b_seconds = gap_c_seconds = NULL`. The `rebuild_interval`
  table is populated for them normally.

### Cross-validation against `advisory_rpm_mapping`

The Red Hat Container Catalog API populated a `repositories[].comparison
.advisory_rpm_mapping` field on image records until ~November 2024 (spike
§13.4). The catalog collector (WP-06) preserves this field in
`catalog_advisory_mapping`. The reconstruction pipeline emits a
cross-check: for every legacy `(image_id, advisory_id)` pair, does our
computation also bind that RHSA to that image? The match rate is
reported on every `cadence analyze reconstruct` run.

The cross-check is informational only — the legacy mapping has its own
quirks (a fix may be tagged against an RHBA-as-bugfix while we link it
to an RHSA, or vice versa), so a 100% match rate isn't the goal. A low
match rate (well under the spec's 95% target) is a signal something has
drifted in either the collector or the reconstructor.

## 10. Verification & authoritative sources

CADENCE cross-checks the database against the live registry via
`cadence verify image REPO:TAG` and `cadence verify random --sample N`. Both
commands wrap `skopeo inspect` (WP-08); the tool is a soft dependency. When
`skopeo` is absent, verification reports `skopeo_unavailable` and exits
normally — the dataset is unaffected.

When a verification surfaces a discrepancy, **the Red Hat Container Catalog
API is the authoritative source.** Reasons:

* The catalog is the upstream record-of-truth for image metadata
  (architecture, advisory mapping, RPM manifest). The registry serves
  whatever artifact a `docker pull` would resolve, and that may have drifted
  for benign reasons (re-tag, mirror lag, manifest list rewrite).
* Skopeo gives a point-in-time snapshot. The catalog stores the canonical
  history the dataset is built on.
* A divergence between skopeo and the catalog means *something is worth
  looking at*, not that CADENCE should rewrite its row.

Verification records discrepancies in the run output; no rows are modified
on the basis of a single skopeo snapshot. Operators investigating a
discrepancy should run `cadence collect catalog --repos REPO,...` to
re-pull the canonical metadata.

## 11. Known limitations

* **Gap A is forward-only.** Pre-flight spike confirmed `cdn-ubi.redhat.com`
  exposes only current repodata.
* **Quay v1 has no RPM-level Gap C.** The Quay collector (WP-07) records
  inter-build interval only. Quay's registry doesn't expose anything analogous
  to the Red Hat Container Catalog's `rpm-manifest` endpoint, and extracting
  RPMs from registry-mounted image layers is out of scope for v1. Quay
  `container_image` rows therefore have populated `tier`, `digest`,
  `architecture`, and `build_date`, but no `container_image_rpm` rows. Analysis
  in WP-09/WP-10 inserts `NULL` into `gap_measurement.gap_a/b/c_seconds` for
  Quay images and populates `rebuild_interval` normally. Per-image RPM
  extraction is deferred to v2.
* **Polling interval bounds Gap A precision.** A four-hour polling interval
  means Gap A measurements are accurate to ±4 hours.
* **Quay build_date is push time, not image-build time.** The Quay collector
  uses each tag's `start_ts` (the moment Quay accepted the push), not the
  `created` timestamp from the per-arch config blob. The two are typically
  within seconds of each other, and `start_ts` is identical across the
  children of a manifest list — what users perceive as the "release time."
  WP-10 inter-build interval calculations are therefore push-time based.
