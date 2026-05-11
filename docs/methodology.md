# CADENCE Methodology

> **Status:** WP-02 stub. Full methodology delivered in WP-13.

This document describes how CADENCE collects data and computes the patch-latency
gaps defined in [`CADENCE-SPEC.md`](../CADENCE-SPEC.md). It is the canonical
reference for anyone reproducing CADENCE findings.

## 1. Data sources

All data sources are public and require no authentication. See `CADENCE-SPEC.md`
§3 for the authoritative list of endpoints. Collectors record raw upstream
responses in `raw_json` columns so that any derived metric can be recomputed
without re-fetching.

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

> Filled in by WP-09.

Package version comparisons use `rpm.labelCompare()` from the system
`python3-rpm` bindings, never string comparison. This handles epoch, version,
and release components correctly per RPM semantics.

## 6. Gap definitions

> Filled in by WP-09, WP-10, and WP-13.

* **Gap A** — RHSA publication to fixed RPM available in
  `cdn-ubi.redhat.com`. **Forward-only.** Pre-flight spike (CADENCE-SPEC.md
  §13.5) confirmed `cdn-ubi.redhat.com` exposes only current repodata, with
  no archive. Gap A precision is bounded by the repodata polling interval:
  with WP-14's default 4-hour timer, Gap A is accurate to ±4 hours, and any
  reported value should be read as "no later than X" rather than "exactly X".
  RPM `build_time` and `file_time` from `<package><time .../></package>` in
  `primary.xml` give a tighter lower bound on availability when present.
* **Gap B** — fixed RPM available to first rebased base image.
* **Gap C** — RHSA publication to first downstream image containing the fix
  (end-to-end), computed per tier.

The repodata collector (WP-05) does not consume `updateinfo.xml`: UBI
repositories publish an empty `updateinfo.xml` (validated in the pre-flight
spike, CADENCE-SPEC §13.6). RHSA→package mapping comes from the
`rhsa_package_fix` table (WP-03) joined to `repo_package` by NEVRA.

## 7. Inter-build interval

> Filled in by WP-09 and WP-10.

For each `(repository, architecture)` we sort observed builds by build date
and record the gap between consecutive builds as a `rebuild_interval` row.
Reported as median, p25/p75, p90/p95/p99, and mean across slices.

## 8. Tier definitions

See `CADENCE-SPEC.md` §6 and `cadence/targets.py`. Tier is the primary slicing
dimension behind the headline finding (UBI ≈ OCP platform fast; layered Red
Hat products slow; Quay-hosted content variable).

## 9. Edge cases

> Filled in by WP-09.

## 10. Known limitations

* **Gap A is forward-only.** Pre-flight spike confirmed `cdn-ubi.redhat.com`
  exposes only current repodata.
* **Quay v1 has no RPM-level Gap C.** Inter-build interval only; per-image
  RPM extraction deferred to v2.
* **Polling interval bounds Gap A precision.** A four-hour polling interval
  means Gap A measurements are accurate to ±4 hours.
