# Threats to validity

This is the credibility document. It is intentionally specific rather
than perfunctory. Each section describes a way the published numbers
could be wrong or misleading, what CADENCE does to mitigate it, and
what kinds of conclusions are or aren't supported in light of it.

If you cite CADENCE in research, read this file too. The dataset is
honest about what it measures; the rest is up to the reader.

## 1. Selection bias

The tracked-repository set in `cadence/targets.py` is **curated, not
exhaustive**. It was assembled from CADENCE-SPEC.md §6 and reflects three
judgement calls:

1. **UBI is canonically the entire UBI 8/9/10 standard/minimal/micro
   variant set,** plus `ubi9/ubi-init`. This is meant to be exhaustive
   for the base-image tier.
2. **OpenShift platform is a curated four-repo subset.** `ose-cli`,
   `ose-installer`, `ose-haproxy-router`, and `ose-kube-rbac-proxy`
   were chosen as representative; OpenShift ships dozens more
   platform images we don't track. Conclusions about
   "the OpenShift platform tier" are valid for these four images
   specifically; broader claims require more sampling.
3. **Layered Red Hat products picks one or two repos from each major
   layered offering** (RHACM, MCE, Logging, ODF, Service Mesh). This
   underweights any layered product that ships many independent
   images. If a single repo within one of these products has an
   unusually slow rebuild discipline, our `rh_layered` distribution
   will overweight it.
4. **Quay community is ten high-profile projects** that operators
   commonly run on OpenShift. It is not a random sample of Quay.

What CADENCE does about this:

* The `tracked_repository` table records the rationale for every
  tracked repo, so the bias is auditable from the dataset alone.
* Tier definitions in [`methodology.md`](methodology.md) §8 spell
  out the per-tier scope.
* The reproducing-findings doc explains that materially-different
  numbers may stem from a different `targets.py`.

What CADENCE doesn't do:

* It doesn't survey or compare against other distributions (Debian,
  Alpine, Wolfi, Rocky, Alma). The methodology generalises; v1 is
  Red Hat-only.
* It doesn't randomly sample from each tier. The headline finding
  ("layered Red Hat products are the bottleneck") is robust to this
  bias because the bottleneck shows up consistently across every
  layered-tier repo we sample, but any *specific* median (e.g.,
  "RHACM has median 21 days") is an estimate from the sampled repos
  in that tier, not a population statistic over every RHACM image
  Red Hat ships.

## 2. Observation bias (forward-only Gap A)

`cdn-ubi.redhat.com` publishes only the current repodata snapshot, with
no archive. CADENCE polls at a 4-hour cadence by default (WP-14
systemd timer). This means:

* **Gap A is bounded by the polling interval.** A fix that lands in
  cdn-ubi 10 minutes after publication and another that lands 3 hours
  50 minutes after publication can be indistinguishable at our
  resolution. Reported Gap A values should be read as "no later than X".
* **Gap A is unmeasured for any RHSA whose fix landed in cdn-ubi
  before our forward polling started.** The `repo_first_seen_at`
  anchor doesn't exist for those records, so the corresponding
  `gap_a_seconds` is NULL.
* **Gap B and Gap C can still be computed** for those RHSAs, since
  they depend on `image_first_built_at` not on the cdn-ubi anchor.

What CADENCE does about this:

* The RPM-level `<package><time .../></package>` element gives a
  tighter lower bound on availability when present; the repodata
  collector captures it (`repo_package.build_time`,
  `repo_package.file_time`).
* The methodology section §6 calls this out explicitly with an
  example.

What CADENCE doesn't do:

* It doesn't sub-poll. Sub-hour cadence would burn host capacity on a
  shared homelab box for diminishing returns once Gap A has been
  characterised. Users who want minute-level Gap A precision can
  override `CADENCE_RATE_LIMIT_PER_HOST_SECONDS` and the WP-14 timer
  intervals.

## 3. Survivorship bias

The Red Hat Container Catalog and Quay both index *currently published*
content. Both will occasionally drop tags (e.g., for security or
vendor-policy reasons). If a tag was published and later removed before
CADENCE's first scan, that tag is invisible to us.

Effect on the dataset:

* The set of `container_image` rows is biased toward content that
  survived to the moment of collection. Builds that were published
  and quickly retracted are systematically under-represented.
* Inter-build interval calculations operate on `build_date` ordering;
  a retracted intermediate build leaves a gap that looks like a
  longer-than-real interval.

What CADENCE does:

* `raw_json` preserves the catalog/Quay record verbatim, so any
  later analysis that wants to detect retractions can compare
  successive raw snapshots.
* The `tracked_repository.added_at` and per-image `collected_at`
  timestamps let downstream tooling reason about which content was
  visible at which point in time.

What we don't know:

* The retraction rate. It would be quantifiable with a long-running
  CADENCE deployment that snapshots `container_image` rows over time
  and diffs them; v1 doesn't do this.

## 4. Catalog ingestion lag

Images become available on `registry.access.redhat.com` (and Quay)
some amount of time before they appear in the catalog API's
`creation_date` field. CADENCE measures the catalog's view, not the
registry's, so for the catalog-source tiers (UBI / OCP / layered):

* Reported `build_date` may be slightly later than when the build was
  actually publicly available.
* This systematically inflates Gap B and Gap C by the catalog
  ingestion lag.

What CADENCE does:

* `cadence verify random --sample N` cross-checks a sample of
  `(repository, tag)` pairs against `skopeo inspect`. Discrepancies
  are reported; the catalog API remains authoritative in the
  database (we don't overwrite rows based on a single registry
  snapshot — see methodology §10).
* For Quay rows, the `start_ts` we use *is* the registry's view, so
  this particular lag doesn't apply there.

What we don't know:

* The exact lag distribution. Conservatively, treat any single
  catalog-source Gap B / Gap C measurement under ~1 hour with
  suspicion — the catalog ingestion path is usually faster than that
  but isn't documented to be.

## 5. Legacy `advisory_rpm_mapping` deprecation

The catalog API populated a `repositories[].comparison
.advisory_rpm_mapping` field on image records until ~November 2024,
then stopped (CADENCE-SPEC.md §13.4). Pre-Nov-2024 records carry the
field; post-Nov-2024 records don't.

CADENCE uses this field strictly as a **cross-validation signal**, not
as ground truth:

* `cadence analyze reconstruct` computes RHSA→image bindings from
  `rhsa_package_fix` and `container_image_rpm` directly, via NEVRA
  comparison (methodology §5).
* For images with `catalog_advisory_mapping` rows, the
  reconstruction reports a match rate. A low rate (well under 95%)
  signals drift in either CADENCE's parser or the legacy field's
  semantics.

The match rate is informational; we never rewrite a row on the basis
of disagreement.

## 6. UBI `updateinfo.xml` is empty

UBI repositories publish an empty `updateinfo.xml`, unlike entitled
RHEL repositories that ship the RHSA→package mapping there.
Pre-flight (spike §13.6) confirmed this; CADENCE-SPEC.md §6 records
it as a non-goal.

Effect: CADENCE's RHSA→package mapping comes from
`rhsa_package_fix` (the CSAF document), not from UBI's
`updateinfo.xml`. Both should converge in principle; we use the CSAF
source because (a) it's the authoritative one and (b) the UBI source
doesn't exist.

## 7. Quay v1 has no RPM-level Gap C

Quay images don't expose anything analogous to the Container Catalog's
`rpm-manifest` endpoint, and extracting RPMs from registry-mounted
image layers is out of scope for v1. The consequence:

* `gap_measurement` rows for Quay repositories have NULL `image_id`,
  `image_first_built_at`, `gap_b_seconds`, and `gap_c_seconds`.
* `rebuild_interval` rows for Quay repositories *are* populated;
  inter-build cadence is the headline metric Quay supports in v1.

Any cross-tier comparison of Gap C that includes Quay rows must
exclude `tier IN ('quay_redhat', 'quay_community', 'quay_partner')`
from the Gap C set. The reporting layer does this implicitly by
filtering on `gap_c_seconds IS NOT NULL`.

## 8. What the data does and does not support

**It supports** statements about *publicly observable* patch latency
in the specific repositories CADENCE tracks, at a precision bounded
by the polling cadence. The headline finding ("layered Red Hat
products are the bottleneck") is robust at the tier level:
RHACM/MCE/Service Mesh consistently sit dozens of days above UBI in
every meaningful slice we've checked.

**It does not support** statements about:

* *Internal* rebuild schedules at Red Hat or any other vendor.
* The user impact of any specific delay. Patch latency is one input
  to security posture; whether a 26-day median is "fine" or "bad"
  depends on threat model, compensating controls, and the
  organisation's update discipline. CADENCE measures the latency, not
  the verdict.
* Causation. The data shows *that* layered products have longer
  rebuild cycles, not *why*. Plausible causes (test surface, release
  qualification, downstream dependencies, vendor-prioritised content
  signing) are hypotheses, not findings.
* Any other distribution. Debian, Alpine, Wolfi, Rocky, Alma — the
  methodology generalises but the data is Red Hat only in v1.

When in doubt, cite the methodology + dataset together: the
`manifest.json` records exactly which records, which methodology
version, and which CADENCE version produced the numbers you're
quoting.
