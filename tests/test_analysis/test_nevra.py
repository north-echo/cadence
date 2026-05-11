"""NEVRA comparator tests.

Includes Hypothesis property-based tests per WP-09 acceptance:
"NEVRA comparison handles epoch/version/release correctly".

We test the pure-Python ``_python_label_compare`` explicitly so the property
tests don't silently no-op on hosts where the C ``rpm`` module is available;
the module-level ``label_compare`` binding is tested separately for the
canonical known-case set.
"""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from cadence.analysis import nevra
from cadence.analysis.nevra import (
    _python_label_compare,
    evr_ge,
    evr_lt,
    label_compare,
    parse_evr,
    rpmvercmp,
)

# ----------------------------------------------------------------------
# rpmvercmp — canonical cases
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "a, b, expected",
    [
        ("1.0", "1.0", 0),
        ("1.0", "1.1", -1),
        ("1.1", "1.0", 1),
        ("1.0.0", "1.0", 1),
        ("1.0", "1.0a", -1),        # extra alpha token = newer (only ~ pre-releases)
        ("1.0a", "1.0", 1),         # symmetric
        ("2", "10", -1),            # purely numeric: by value, not lexicographic
        ("1.01", "1.1", 0),         # leading zeros stripped → equal
        ("1.0", "1.0~rc1", 1),      # tilde sorts before
        ("1.0~rc1", "1.0~rc2", -1),
        ("1.0~rc1", "1.0", -1),
        ("1.0", "1.0^post1", -1),   # caret sorts before end-of-string
        ("1.0^post1", "1.0", 1),    # symmetric
        ("1.0a", "1.0b", -1),
        ("1.0-1", "1.0-2", -1),     # separator ignored, last token differs
        ("1.0.0.0", "1.0", 1),
    ],
)
def test_rpmvercmp_known_cases(a: str, b: str, expected: int) -> None:
    assert rpmvercmp(a, b) == expected


# ----------------------------------------------------------------------
# Properties — reflexivity, antisymmetry
# ----------------------------------------------------------------------


# Generate plausible-looking version strings: tokens separated by '.', '-',
# '_', '~', '^'. Tokens are short alnum runs. Hypothesis explores edge cases
# around tildes and carets.
_token = st.from_regex(r"[A-Za-z0-9]{1,5}", fullmatch=True)
_sep = st.sampled_from([".", "-", "_", "~", "^"])
_version = st.builds(
    lambda toks, seps: toks[0]
    + "".join(s + t for s, t in zip(seps, toks[1:], strict=False)),
    st.lists(_token, min_size=1, max_size=5),
    st.lists(_sep, min_size=0, max_size=10),
).filter(lambda s: bool(s))


@given(s=_version)
@settings(max_examples=200, deadline=None)
def test_rpmvercmp_reflexive(s: str) -> None:
    assert rpmvercmp(s, s) == 0


@given(a=_version, b=_version)
@settings(max_examples=400, deadline=None)
def test_rpmvercmp_antisymmetric(a: str, b: str) -> None:
    c = rpmvercmp(a, b)
    d = rpmvercmp(b, a)
    # sign(cmp(a,b)) == -sign(cmp(b,a))
    assert (c > 0) == (d < 0)
    assert (c == 0) == (d == 0)


@given(a=_version, b=_version)
@settings(max_examples=200, deadline=None)
def test_rpmvercmp_output_in_minus1_0_1(a: str, b: str) -> None:
    assert rpmvercmp(a, b) in (-1, 0, 1)


# ----------------------------------------------------------------------
# label_compare — epoch dominates, then version, then release
# ----------------------------------------------------------------------


def test_label_compare_epoch_dominates() -> None:
    # Higher epoch wins even when version/release look "lower"
    assert _python_label_compare(("2", "0.1", "1.el9"), ("0", "9.9", "9.el9")) == 1
    assert _python_label_compare(("0", "9.9", "9.el9"), ("2", "0.1", "1.el9")) == -1


def test_label_compare_version_dominates_release() -> None:
    assert _python_label_compare(("0", "1.0", "1.el9"), ("0", "0.9", "99.el9")) == 1


def test_label_compare_release_breaks_tie() -> None:
    assert _python_label_compare(("0", "1.0", "1.el9"), ("0", "1.0", "2.el9")) == -1
    assert _python_label_compare(("0", "1.0", "1.el9"), ("0", "1.0", "1.el9")) == 0


def test_label_compare_treats_empty_epoch_as_zero() -> None:
    assert _python_label_compare((None, "1.0", "1"), ("0", "1.0", "1")) == 0
    assert _python_label_compare(("", "1.0", "1"), ("0", "1.0", "1")) == 0


# ----------------------------------------------------------------------
# parse_evr + evr_ge / evr_lt
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "evr, expected",
    [
        ("0:1.0-1.el9", ("0", "1.0", "1.el9")),
        ("1:2.3-4", ("1", "2.3", "4")),
        ("1.0-1.el9", ("0", "1.0", "1.el9")),   # no epoch
        ("1.0", ("0", "1.0", "")),               # no epoch, no release
    ],
)
def test_parse_evr(evr: str, expected: tuple[str, str, str]) -> None:
    assert parse_evr(evr) == expected


def test_evr_ge_and_lt() -> None:
    fixed = "0:1.0-1.el9"
    assert evr_ge("0:1.0-1.el9", fixed) is True
    assert evr_ge("0:1.0-2.el9", fixed) is True
    assert evr_ge("0:1.1-1.el9", fixed) is True
    assert evr_ge("1:0.1-1.el9", fixed) is True  # higher epoch
    assert evr_ge("0:0.9-9.el9", fixed) is False
    assert evr_lt("0:0.9-9.el9", fixed) is True


# ----------------------------------------------------------------------
# Selected implementation must agree with the pure-Python one on goldens
# ----------------------------------------------------------------------


def test_module_level_label_compare_matches_python_on_known() -> None:
    cases: list[tuple[tuple[str, str, str], tuple[str, str, str]]] = [
        (("0", "1.0", "1.el9"), ("0", "1.0", "1.el9")),
        (("0", "1.0", "1.el9"), ("0", "1.1", "1.el9")),
        (("0", "1.1", "1.el9"), ("0", "1.0", "1.el9")),
        (("1", "0.1", "1"), ("0", "9.9", "9")),
    ]
    for a, b in cases:
        assert label_compare(a, b) == _python_label_compare(a, b)


def test_module_level_label_compare_exists() -> None:
    assert callable(nevra.label_compare)
