# Implementation notes

Per CADENCE-SPEC.md §14 ("When ambiguity arises between this spec and
discovered reality… prefer reality and document the discrepancy"), this file
records places where the live world has diverged from the spec since it was
written.

## CVRF → CSAF v2 migration (WP-03)

**Date discovered:** 2026-05-10
**Spec text:** §3 and §WP-03 reference
`https://access.redhat.com/hydra/rest/securitydata/cvrf.json` and
`/cvrf/{RHSA-ID}.json`.

**Reality:** Red Hat retired the CVRF endpoint. The new list endpoint is
`csaf.json`; the detail endpoint is `csaf/{RHSA}.json`. Both return CSAF v2
documents, which are richer than the old CVRF format (proper VEX product
statuses, structured CWE references, full revision history).

**What we did:** `cadence/collectors/rhsa.py` uses the CSAF endpoints. The data
model in `001_initial.sql` did not need to change — `rhsa`, `rhsa_cve`, and
`rhsa_package_fix` map cleanly to the CSAF fields. Test fixtures in
`tests/fixtures/rhsa/` are real captures from the CSAF endpoint.

**WP-04 implication:** §WP-04 already pointed at
`/security/data/csaf/v2/advisories/{rhsa-lower}.json` for VEX statements. The
WP-03 detail endpoint and the WP-04 endpoint return the same CSAF document
(byte-formatting differs, JSON content is identical). WP-04 nonetheless fetches
from the spec'd URL so it can run standalone (e.g., to backfill VEX for RHSAs
already in the database). The spec'd `access.redhat.com` path 301-redirects to
`security.access.redhat.com`; `HTTPClient` follows redirects transparently.

## RHSA CSAF docs only carry `fixed` (WP-04)

**Date discovered:** 2026-05-10
**Spec text:** §WP-04 acceptance: "Test fixtures cover all four VEX status
values" (`fixed`, `affected`, `not_affected`, `under_investigation`).

**Reality:** RHSA-level CSAF v2 documents only carry `fixed` product_status in
practice — an RHSA exists precisely to announce a fix. The other three
statuses live in CVE-level CSAF documents (`/v2/csaf/{cve}.json`), which are
out of scope for WP-04 as currently specified.

**What we did:** The parser handles all four CSAF status buckets (per the
mapping in `cadence/collectors/csaf.py`). Acceptance is exercised via a
synthetic fixture (`tests/fixtures/rhsa/synthetic_all_statuses.json`) that
covers all four, plus a real-data test asserting the observed `fixed`-only
shape of real RHSAs. If we later decide we want non-`fixed` VEX coverage,
extend WP-04 (or add a WP-04b) to pull CVE-level CSAF documents.

## sqlite3 default TIMESTAMP converter (WP-03)

**Date discovered:** 2026-05-10
**Reality:** Python 3.12 deprecated `detect_types=PARSE_DECLTYPES`'s default
TIMESTAMP converter, and the converter never handled `+00:00` offsets anyway.

**What we did:** Dropped `detect_types` from `cadence/db.py`. Timestamps are
stored and returned as ISO 8601 strings. Callers that need `datetime` objects
parse on read (cheap, and avoids the legacy converter's offset bug).
