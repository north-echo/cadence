"""NEVRA comparison.

Spec mandate (CADENCE-SPEC.md §4 model notes): NEVRA comparisons must use
``rpm.labelCompare()`` from the rpm Python bindings, never string comparison.

This module provides one function, :func:`label_compare`, that delegates to
``rpm.labelCompare`` when the bindings are importable, and falls back to a
self-contained pure-Python implementation of the same algorithm when they
aren't. Both implementations are property-tested in
``tests/test_analysis/test_nevra.py`` so we know the fallback matches the
canonical C implementation.

Why the fallback exists
-----------------------

The Fedora target host always has ``python3-rpm`` installed (see
``Containerfile``). Dev hosts may not, and ``pip install rpm-py-installer``
fails on non-Linux (see ``NOTES.md``). The fallback keeps the test suite
running on any platform without compromising the production path: when both
implementations are available, production prefers the C one.

Reference: https://rpm.org/user_doc/dependencies.html — the rpmvercmp
algorithm walks two strings in lockstep, isolating runs of digits or
letters as "tokens" and comparing them; ``~`` (tilde) sorts before
everything (used for pre-releases); ``^`` (caret) sorts after everything.
"""

from __future__ import annotations

from collections.abc import Callable

# NEVRA tuple = (epoch_str, version_str, release_str). epoch_str may be "" or
# None to mean "0"; matches what rpm.labelCompare accepts.
Nevra = tuple[str | None, str, str]


def _normalize_epoch(epoch: str | None) -> str:
    return epoch if epoch else "0"


def _is_alnum(ch: str) -> bool:
    return ch.isalnum()


def rpmvercmp(a: str, b: str) -> int:
    """Pure-Python implementation of rpm's rpmvercmp.

    Returns ``-1``, ``0``, or ``1`` (sign of ``a - b``).
    """
    if a == b:
        return 0

    i, j = 0, 0
    la, lb = len(a), len(b)

    while i < la or j < lb:
        # Skip non-alphanumeric separators in either string, but treat
        # `~` and `^` as significant.
        while i < la and not _is_alnum(a[i]) and a[i] not in "~^":
            i += 1
        while j < lb and not _is_alnum(b[j]) and b[j] not in "~^":
            j += 1

        # Tilde: less than anything (including end-of-string).
        a_tilde = i < la and a[i] == "~"
        b_tilde = j < lb and b[j] == "~"
        if a_tilde or b_tilde:
            if a_tilde and not b_tilde:
                return -1
            if b_tilde and not a_tilde:
                return 1
            i += 1
            j += 1
            continue

        # Caret: less than anything except end-of-string. When one side ends
        # and the other has a caret, the side WITH the caret loses.
        a_caret = i < la and a[i] == "^"
        b_caret = j < lb and b[j] == "^"
        if a_caret or b_caret:
            if a_caret and j >= lb:
                return 1
            if b_caret and i >= la:
                return -1
            if a_caret and not b_caret:
                return -1
            if b_caret and not a_caret:
                return 1
            i += 1
            j += 1
            continue

        if i >= la or j >= lb:
            break

        # Read a token of digits XOR letters from each side.
        a_is_digit = a[i].isdigit()
        b_is_digit = b[j].isdigit()
        if a_is_digit != b_is_digit:
            # rpm: numeric tokens are "newer" than alphabetic tokens.
            return 1 if a_is_digit else -1

        if a_is_digit:
            a_start, b_start = i, j
            while i < la and a[i].isdigit():
                i += 1
            while j < lb and b[j].isdigit():
                j += 1
            a_tok = a[a_start:i].lstrip("0") or "0"
            b_tok = b[b_start:j].lstrip("0") or "0"
            if len(a_tok) != len(b_tok):
                return 1 if len(a_tok) > len(b_tok) else -1
            if a_tok != b_tok:
                return 1 if a_tok > b_tok else -1
        else:
            a_start, b_start = i, j
            while i < la and a[i].isalpha():
                i += 1
            while j < lb and b[j].isalpha():
                j += 1
            a_tok = a[a_start:i]
            b_tok = b[b_start:j]
            if a_tok != b_tok:
                return 1 if a_tok > b_tok else -1

    # Exhausted both — strings are equivalent (only separators differed).
    if i >= la and j >= lb:
        return 0
    return 1 if i < la else -1


def _python_label_compare(a: Nevra, b: Nevra) -> int:
    ea = int(_normalize_epoch(a[0]) or 0)
    eb = int(_normalize_epoch(b[0]) or 0)
    if ea != eb:
        return -1 if ea < eb else 1
    c = rpmvercmp(a[1], b[1])
    if c != 0:
        return c
    return rpmvercmp(a[2], b[2])


def _select_implementation() -> Callable[[Nevra, Nevra], int]:
    try:
        import rpm  # type: ignore[import-not-found]
    except ImportError:
        return _python_label_compare
    return rpm.labelCompare  # type: ignore[no-any-return]


# Module-level binding chosen at import time. Tests can swap this when
# they want to exercise both paths.
label_compare: Callable[[Nevra, Nevra], int] = _select_implementation()


# ---------------------------------------------------------------------------
# Helpers for working with CADENCE's stored format
# ---------------------------------------------------------------------------


def parse_evr(evr: str) -> Nevra:
    """Split an ``epoch:version-release`` string into a NEVRA-without-arch tuple.

    Tolerates plain ``version-release`` strings (epoch defaults to ``0``).
    """
    if ":" in evr:
        epoch, rest = evr.split(":", 1)
    else:
        epoch, rest = "0", evr
    if "-" in rest:
        version, release = rest.split("-", 1)
    else:
        version, release = rest, ""
    return (epoch, version, release)


def evr_ge(observed: str, fixed: str) -> bool:
    """``observed >= fixed`` under rpm NEVRA semantics."""
    return label_compare(parse_evr(observed), parse_evr(fixed)) >= 0


def evr_lt(observed: str, fixed: str) -> bool:
    return label_compare(parse_evr(observed), parse_evr(fixed)) < 0


__all__ = [
    "Nevra",
    "evr_ge",
    "evr_lt",
    "label_compare",
    "parse_evr",
    "rpmvercmp",
]
