"""Tests for the CLI exit-code policy after collector runs."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from cadence.cli import _finish_collect_run


@dataclass
class _FakeResult:
    records: int
    errors: list[str]


def test_finish_zero_records_with_errors_exits_one() -> None:
    """Catastrophic run — nothing persisted, errors present — must exit 1."""
    with pytest.raises(SystemExit) as exc_info:
        _finish_collect_run(_FakeResult(records=0, errors=["a", "b"]), source="rhsa")
    assert exc_info.value.code == 1


def test_finish_records_with_errors_exits_zero() -> None:
    """Successful run with some per-record errors: NOT a failure."""
    # No SystemExit raised
    _finish_collect_run(_FakeResult(records=5442, errors=["err1", "err2", "err3"]),
                        source="catalog")


def test_finish_zero_records_zero_errors_exits_zero() -> None:
    """Empty incremental run — nothing to do — is fine."""
    _finish_collect_run(_FakeResult(records=0, errors=[]), source="catalog")


def test_finish_records_zero_errors_exits_zero() -> None:
    _finish_collect_run(_FakeResult(records=100, errors=[]), source="rhsa")


def test_finish_truncates_long_error_lists_in_output(
    capsys: pytest.CaptureFixture[str],
) -> None:
    errors = [f"err-{i}" for i in range(15)]
    _finish_collect_run(_FakeResult(records=1000, errors=errors), source="rhsa")
    err = capsys.readouterr().err
    # First 10 shown, remainder summarised
    for i in range(10):
        assert f"err-{i}" in err
    assert "and 5 more" in err
