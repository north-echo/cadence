# How long does a security fix actually take to reach your container?

> **Draft v0.** Sections 3–6 are placeholders pending the full forward-data
> collection. The prose below is the data-independent skeleton.

## 1. The question almost nobody asks

When Red Hat publishes a Critical CVE for `glibc`, the engineering response
inside Red Hat is fast: a fix lands in RHEL, an RHSA is published, the
errata flows out through every channel they have. You can read about it on
the Red Hat Security blog by lunchtime.

The question almost nobody asks: **when does the patched container image
that's actually running on your cluster get rebuilt?**

It's a different question. The RHSA tells you the fix exists. It does
not tell you the fix has reached the binary your workload is calling.
That's a journey through several rebuild boundaries, each one owned by a
different team with its own schedule. Most discussions of container
security treat patches as a binary state: patched or not. Reality is a
staircase.

We instrumented every public hop in the Red Hat container supply chain
and measured it for [time window TBD]. This is what we found.

## 2. Four hops, made concrete

Pick a real fix to follow end-to-end: **RHSA-2025:0850**, a sandbox
escape in `python-jinja2` (CVE-2024-56326). It's a small, focused
advisory — one CVE, two binary RPMs — which makes the chain of custody
easy to trace.

**Hop 1 — RHSA publication.** Red Hat Product Security publishes
`RHSA-2025:0850` at `2025-01-30T18:06:01Z`. The CSAF v2 document is
public at
`https://access.redhat.com/hydra/rest/securitydata/csaf/RHSA-2025:0850.json`.
It lists the fixed package: `python3-jinja2-0:2.11.3-6.el9_4.noarch` for
the RHEL 9.4 EUS AppStream product.

> 🕒 *t = 0. The fix exists, but nothing has been rebuilt yet.*

**Hop 2 — UBI base image (cdn-ubi.redhat.com).** RHEL is upstream of
UBI. Some interval later, a UBI 9 baseos repodata snapshot at
`https://cdn-ubi.redhat.com/content/public/ubi/dist/ubi9/9/x86_64/baseos/os/`
contains the fixed `python3-jinja2-2.11.3-6.el9_4.noarch.rpm`. We call
this **Gap A**: the time from RHSA publication to the patched RPM
being downloadable.

Note the asymmetry — UBI's `repomd.xml` only exposes the *current*
state. There's no archive. If you weren't polling at the right moment,
you can't reconstruct exactly when the RPM landed; you can only bound it
by your polling cadence.

**Hop 3 — UBI container image.** Eventually a new `ubi9/ubi:9.4-NNNN`
tag appears on `registry.access.redhat.com`, built from the updated
repodata. Its RPM manifest now lists the fixed `python3-jinja2`. We
call the new gap **Gap B**: from RPM-in-repo to RPM-in-image.

**Hop 4 — Layered Red Hat products and Quay-hosted content.** RHACM,
OpenShift Logging, ODF, Service Mesh, and every community/partner image
on Quay that builds on top of UBI eventually rebuilds against the new
base. Each one has its own rebuild discipline. The cumulative time
from RHSA publication to the *downstream* image is **Gap C** — the
number that matters for "when does my workload actually pull the
patched binary".

Every timestamp in that chain is publicly available. RHSAs from
`access.redhat.com`, repodata from `cdn-ubi.redhat.com`, image
metadata from `catalog.redhat.com`, Quay tag history from
`quay.io/api/v1/`. CADENCE polls all of them on a polite-rate-limited
schedule, stores the raw JSON for re-analysis, and joins them by NEVRA
to produce one `gap_measurement` row per `(advisory, repository,
architecture, package)`.

---

## 3. Headline finding: the staircase

> *[PLACEHOLDER — needs full backfill + forward collection]*
>
> Insert: `headline_gap_c_by_tier.png` (box plot of Gap C by tier).
>
> Insert: 1–2 paragraphs reading the chart. Expected shape based on
> pre-flight: UBI and OCP platform medians sit near the floor (~5–7
> days), `rh_layered` adds a meaningful step (~16–26 days median,
> p90 40–60+ days), Quay community is variable.

## 4. Why are layered products slower?

> *[PLACEHOLDER — write once §3 numbers are real]*
>
> Speculation-but-flag-it: integration testing surface, downstream
> dependency depth, smaller per-product engineering teams,
> release-qualification gates. CADENCE measures the *what*. The *why*
> is hypothesis. Frame it that way.

## 5. The optimistic note: rebuild cadence is accelerating

> *[PLACEHOLDER — needs full backfill]*
>
> Insert: `interval_monthly_median_by_tier.png` (monthly median
> inter-build interval per tier, over time).
>
> Expected: UBI 9 amd64 median interval has compressed from ~12 days
> (lifetime) to ~5 days (last 12 months); OCP platform tracks this
> trajectory closely. Hops 2 and 3 are getting faster.

## 6. What this means if you operate OpenShift

> *[PLACEHOLDER — sharpen once §3 numbers are real]*
>
> The practical takeaway: your real exposure window is the tier
> *ceiling*, not the floor. If your cluster runs RHACM, your effective
> Gap C for any RHEL CVE that touches RHACM is dominated by RHACM's
> rebuild cadence, not by UBI's. Patching SLOs that assume the UBI
> floor underestimate exposure. Concrete (non-prescriptive) advice
> here.

---

## 7. Methodology, in one paragraph

CADENCE uses only public, unauthenticated data sources: Red Hat's CSAF
v2 advisories for RHSAs, `cdn-ubi.redhat.com` repodata for UBI
package availability, the Red Hat Container Catalog API for image
metadata and RPM manifests, and Quay's REST + OCI v2 endpoints for
Quay-hosted content. Every collector preserves the raw upstream
payload so any derived metric can be recomputed without re-fetching.
Package version comparisons use `rpm.labelCompare()` from the
canonical `rpm` library — never string comparison. Analyses are
versioned via a `methodology_version` column so multiple
interpretations of the same raw data coexist in the database. The
full methodology, including how we handle each edge case (RHSAs that
predate forward polling, multi-arch fan-out, VEX `not_affected`
exclusions, the legacy `advisory_rpm_mapping` field's deprecation in
November 2024), is documented at
[`docs/methodology.md`](https://github.com/north-echo/cadence/blob/main/docs/methodology.md).

## 8. What the data does *not* support

CADENCE measures *publicly observable* patch latency in the specific
repositories it tracks, at a precision bounded by its polling cadence
(4 hours by default). The headline tier comparison is robust at the
tier level — the staircase shape shows up consistently across every
meaningful slice. Beyond that, be careful what you read into the
numbers:

- **CADENCE doesn't measure *internal* rebuild schedules** at Red Hat
  or any other vendor. It measures the public surface only.
- **It doesn't pass judgement on whether a given latency is acceptable.**
  "26-day median" is a number, not a verdict; what counts as
  acceptable depends on threat model, compensating controls, and the
  organisation's patching discipline.
- **It doesn't claim causation.** The data shows *that* layered
  products have longer rebuild cycles. Plausible causes (testing
  surface, release qualification, vendor priorities) are hypotheses.
- **It doesn't generalise to other distributions.** Debian, Alpine,
  Wolfi, Rocky, Alma — the methodology generalises but v1 is Red Hat
  only.

The dataset is the data. The interpretation is yours.

A more honest discussion of selection bias, observation bias,
survivorship bias, and the other ways the published numbers could be
wrong or misleading lives in
[`docs/threats-to-validity.md`](https://github.com/north-echo/cadence/blob/main/docs/threats-to-validity.md).
We recommend reading it before citing CADENCE.

## 9. Reproduce it yourself

The tool, the dataset, and the methodology are all open. Apache-2.0
for the code, CC-BY-4.0 for the dataset.

```bash
# Fedora 42+ (Linux host with Python 3.12 and python3-rpm)
pip install --user 'ne-cadence[export]'
cadence db init

# Backfill — takes ~3 hours at default polite-rate limits
cadence collect rhsa --since 2024-01-01
cadence collect csaf --all-known
cadence collect repodata
cadence collect catalog
cadence collect quay

# Reconstruct gaps + intervals
cadence analyze reconstruct
cadence report summary
cadence report charts --output-dir out/charts
```

Or run it as a continuous collection service via the shipped
user-level systemd timers — see
[`docs/operations.md`](https://github.com/north-echo/cadence/blob/main/docs/operations.md).

The dataset associated with this post is published at Zenodo with
DOI `[TBD]`, including the methodology and a manifest of every input
record with sha256 + byte counts so external verifiers can confirm
nothing has been tampered with after publication.

If you reproduce the numbers and get materially different results,
file an issue. We document the common sources of variance in
[`docs/reproducing-findings.md`](https://github.com/north-echo/cadence/blob/main/docs/reproducing-findings.md);
beyond that, we want to hear what we got wrong.

## 10. What's next

**v2: cross-distribution comparison.** The methodology generalises.
Debian, Alpine, Wolfi, Rocky, Alma — each has its own equivalent of
the four-hop chain we measured for Red Hat. Right now there's no
dataset that lines them up side by side. There should be. We expect
the staircase shape to be universal but the step heights to differ
dramatically across distributions.

**Talk submission.** A version of these findings has been submitted to
KubeCon EU 2027 (with DevConf US and FOSDEM as fallbacks). If you're
on a programme committee and this is interesting to you, the talk
proposal is at [TBD].

**Downstream tooling, informed by these findings.** Knowing *where*
the latency lives changes what an automated container-patching tool
should optimise for. CADENCE makes the case that the value is in the
layered-product slow lane, not in shaving seconds off the UBI fast
lane. That's a separate project (and a separate blog post when it
ships).

In the meantime, CADENCE keeps collecting. The systemd timers run
every 4–12 hours, the dataset gets re-published periodically, and the
methodology becomes the foundation for whatever comes next.

---

*CADENCE is a research project under the North Echo identity. Source
code: [github.com/north-echo/cadence](https://github.com/north-echo/cadence).
Dataset DOI: [TBD]. Questions, corrections, and reproducibility issues
welcome at [TBD].*

---

## Appendix A: Short-form versions

### A.1. Social post (~300 words — LinkedIn / Hacker News body / link share)

> *[Numbers in brackets are placeholders pending the full forward
> collection.]*

Most container-security thinking treats CVE patches as a binary
state: patched or not. The reality is a staircase.

A Red Hat security fix travels through at least four publicly
observable rebuild boundaries before it reaches a workload running
on your cluster: the RHSA itself, the UBI base image consuming from
RHEL, OpenShift or layered Red Hat products rebuilding on UBI, and
finally any Quay-hosted content that depends on those. Each hop has
its own rebuild discipline. Cumulative latency is what users
actually feel.

We measured every one of those hops, end to end, for [N] RHSAs
over [N] months, using only public unauthenticated data sources.
The tool, the dataset, and the methodology are open: Apache-2.0
code, CC-BY-4.0 dataset.

The headline finding: **layered Red Hat products are the
bottleneck.** UBI and the OpenShift platform sit near the floor
(median ~[5-7] days from RHSA to a downstream image carrying the
fix). RHACM, MCE, Service Mesh, ODF, and Logging consistently sit
[3-4×] higher — median ~[16-26] days, p90 ~[40-60] days. The
floor isn't where your exposure lives.

If you operate OpenShift in production, your real patch-latency
ceiling is the slowest tier you depend on, not the fastest. Your
Gap-C window for any CVE that touches RHACM is dominated by RHACM's
rebuild cadence — not by how fast UBI shipped the fix.

There's a lot more in the post — methodology, threats to validity,
how to reproduce, and where this is going next (cross-distro v2,
downstream patching).

→ https://northecho.dev/cadence-patch-latency
→ Dataset (DOI [TBD]): [link]
→ Source: github.com/north-echo/cadence

### A.2. KubeCon / DevConf talk abstract (~180 words)

**Title:** *The staircase: measuring multi-hop patch latency in the
Red Hat container supply chain*

A security fix doesn't reach your workload the moment Red Hat
publishes an RHSA. It traverses at least four publicly observable
rebuild boundaries — RHEL → UBI → OpenShift platform / layered
products → Quay-hosted content — each owned by a different team
with its own rebuild discipline. Most patch-latency discussion
collapses this into a binary patched/unpatched state. The reality
is a staircase, and the step heights vary by more than an order of
magnitude across product tiers.

This talk presents **CADENCE**, an open-source measurement tool and
the dataset it produces: end-to-end patch latency, sliced by tier,
severity, architecture, and time, derived entirely from public
unauthenticated sources. We'll walk through the methodology, share
[N] months of empirical findings (preview: the layered-product tier
dominates everyone's real exposure window), discuss the threats to
validity, and explain why building a faster downstream patcher
makes sense only after you know which lane is actually slow.

Audience: platform engineers, container security teams, and anyone
who has ever written a patching SLO.
