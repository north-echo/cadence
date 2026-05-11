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
WP-03 detail endpoint and the WP-04 endpoint return the same CSAF document via
two different paths; WP-04 may end up reusing the WP-03 detail fetch rather
than going to the alternate URL. Decide when implementing WP-04.

## sqlite3 default TIMESTAMP converter (WP-03)

**Date discovered:** 2026-05-10
**Reality:** Python 3.12 deprecated `detect_types=PARSE_DECLTYPES`'s default
TIMESTAMP converter, and the converter never handled `+00:00` offsets anyway.

**What we did:** Dropped `detect_types` from `cadence/db.py`. Timestamps are
stored and returned as ISO 8601 strings. Callers that need `datetime` objects
parse on read (cheap, and avoids the legacy converter's offset bug).
