"""CADENCE command-line interface.

WP-01 ships subcommand stubs only. Each subcommand prints a not-yet-implemented
message and exits non-zero. Real behavior lands in later work packages:

    collect rhsa      WP-03
    collect csaf      WP-04
    collect repodata  WP-05
    collect catalog   WP-06
    collect quay      WP-07
    verify *          WP-08
    analyze *         WP-09 / WP-10
    report *          WP-11
    export *          WP-12
    health            WP-14
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import click
from rich.console import Console

from cadence import __version__
from cadence.config import Settings
from cadence.db import (
    DEFAULT_DB_PATH,
    apply_migrations,
    connect,
    list_migrations,
)

console = Console()
err_console = Console(stderr=True)


def _not_implemented(name: str) -> None:
    err_console.print(
        f"[yellow]cadence {name}[/yellow]: not implemented yet (stub from WP-01)."
    )
    sys.exit(2)


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, prog_name="cadence")
@click.option(
    "--cache-dir",
    type=click.Path(file_okay=False, path_type=Path),
    envvar="CADENCE_CACHE_DIR",
    help="HTTP cache directory (default: ~/.cache/cadence).",
)
@click.option(
    "--db-path",
    type=click.Path(dir_okay=False, path_type=Path),
    envvar="CADENCE_DB_PATH",
    help=f"SQLite database path (default: {DEFAULT_DB_PATH}).",
)
@click.pass_context
def main(ctx: click.Context, cache_dir: Path | None, db_path: Path | None) -> None:
    """CADENCE: Container Supply Chain Patch Latency Measurement."""
    overrides: dict[str, object] = {}
    if cache_dir is not None:
        overrides["cache_dir"] = cache_dir
    if db_path is not None:
        overrides["db_path"] = db_path
    ctx.obj = Settings(**overrides)  # type: ignore[arg-type]


# ---------- db ----------


@main.group()
def db() -> None:
    """Database lifecycle commands."""


@db.command("init")
@click.pass_obj
def db_init(settings: Settings) -> None:
    """Create the SQLite database and apply all migrations."""
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    with connect(settings.db_path) as conn:
        applied = apply_migrations(conn)
    console.print(
        f"[green]Initialized[/green] {settings.db_path} "
        f"({len(applied)} migration(s) applied)."
    )


@db.command("migrate")
@click.pass_obj
def db_migrate(settings: Settings) -> None:
    """Apply any pending migrations. Idempotent."""
    with connect(settings.db_path) as conn:
        applied = apply_migrations(conn)
    if applied:
        console.print(f"[green]Applied[/green] {len(applied)} migration(s): {', '.join(applied)}")
    else:
        console.print("[green]Up to date.[/green]")


@db.command("status")
@click.pass_obj
def db_status(settings: Settings) -> None:
    """Show migration status."""
    all_migrations = list_migrations()
    with connect(settings.db_path) as conn:
        cur = conn.execute("SELECT name FROM schema_migrations ORDER BY name")
        applied = {row[0] for row in cur.fetchall()}
    for name in all_migrations:
        marker = "[green]✓[/green]" if name in applied else "[yellow]·[/yellow]"
        console.print(f"  {marker} {name}")


# ---------- collect ----------


@main.group()
def collect() -> None:
    """Data collection commands."""


@collect.command("rhsa")
@click.option("--since", type=str, help="ISO date (YYYY-MM-DD). Inclusive.")
@click.option("--until", type=str, help="ISO date (YYYY-MM-DD). Inclusive.")
@click.option("--max-pages", type=int, default=500, show_default=True)
@click.pass_obj
def collect_rhsa(
    settings: Settings, since: str | None, until: str | None, max_pages: int
) -> None:
    """Collect RHSAs from the Red Hat Security Data API."""
    from cadence.collectors.rhsa import RHSACollector

    async def run() -> None:
        async with RHSACollector(settings, settings.db_path) as collector:
            result = await collector.collect(
                since=since, until=until, max_pages=max_pages
            )
            console.print(
                f"[green]rhsa[/green]: {result.records} RHSA(s) persisted "
                f"in {result.duration_seconds:.1f}s "
                f"({len(result.errors)} error(s))"
            )
            if result.errors:
                for msg in result.errors[:10]:
                    err_console.print(f"  [yellow]![/yellow] {msg}")
                if len(result.errors) > 10:
                    err_console.print(f"  … and {len(result.errors) - 10} more")
                sys.exit(1)

    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    with connect(settings.db_path) as conn:
        apply_migrations(conn)
    asyncio.run(run())


@collect.command("csaf")
@click.option("--rhsa", "rhsa_id", type=str, help="Collect a specific RHSA-ID.")
@click.option("--all-known", is_flag=True, help="Collect CSAF for every known RHSA.")
def collect_csaf(rhsa_id: str | None, all_known: bool) -> None:
    """Collect CSAF/VEX documents for known RHSAs. (WP-04)"""
    _not_implemented("collect csaf")


@collect.command("repodata")
@click.option("--repos", type=str, help="Comma-separated repo IDs.")
def collect_repodata(repos: str | None) -> None:
    """Collect cdn-ubi.redhat.com repodata. Forward-only. (WP-05)"""
    _not_implemented("collect repodata")


@collect.command("catalog")
@click.option("--repos", type=str, help="Comma-separated REPO (e.g. ubi9/ubi).")
@click.option("--since", type=str, help="ISO date (YYYY-MM-DD). Incremental.")
def collect_catalog(repos: str | None, since: str | None) -> None:
    """Collect Red Hat Container Catalog images + RPM manifests. (WP-06)"""
    _not_implemented("collect catalog")


@collect.command("quay")
@click.option("--repos", type=str, help="Comma-separated NS/NAME.")
def collect_quay(repos: str | None) -> None:
    """Collect Quay.io tag history + OCI manifests. (WP-07)"""
    _not_implemented("collect quay")


# ---------- verify ----------


@main.group()
def verify() -> None:
    """Registry verification commands. (WP-08)"""


@verify.command("image")
@click.argument("ref")
def verify_image(ref: str) -> None:
    """Cross-validate a single REPO:TAG against the database."""
    _not_implemented("verify image")


@verify.command("random")
@click.option("--sample", "sample", type=int, default=10, show_default=True)
def verify_random(sample: int) -> None:
    """Cross-validate N randomly selected images."""
    _not_implemented("verify random")


# ---------- analyze ----------


@main.group()
def analyze() -> None:
    """Analysis commands."""


@analyze.command("reconstruct")
@click.option("--methodology-version", type=str, help="Methodology version tag.")
def analyze_reconstruct(methodology_version: str | None) -> None:
    """Reconstruct gap_measurement + rebuild_interval from raw data. (WP-09)"""
    _not_implemented("analyze reconstruct")


@analyze.command("gaps")
@click.option("--gap", type=click.Choice(["A", "B", "C"]), help="Restrict to one gap.")
@click.option("--slice-by", "slice_by", type=str, help="Facet to slice by.")
def analyze_gaps(gap: str | None, slice_by: str | None) -> None:
    """Compute gap distributions, optionally sliced by facet. (WP-10)"""
    _not_implemented("analyze gaps")


@analyze.command("intervals")
@click.option("--slice-by", "slice_by", type=str, help="Facet to slice by.")
def analyze_intervals(slice_by: str | None) -> None:
    """Compute inter-build interval distributions. (WP-10)"""
    _not_implemented("analyze intervals")


# ---------- report ----------


@main.group()
def report() -> None:
    """Report generation commands. (WP-11)"""


@report.command("summary")
def report_summary() -> None:
    """Print a Rich-formatted summary of all findings."""
    _not_implemented("report summary")


@report.command("markdown")
@click.option("--output", type=click.Path(dir_okay=False, path_type=Path), required=True)
def report_markdown(output: Path) -> None:
    """Write a comprehensive Markdown report."""
    _not_implemented("report markdown")


@report.command("charts")
@click.option("--output-dir", type=click.Path(file_okay=False, path_type=Path), required=True)
def report_charts(output_dir: Path) -> None:
    """Write publication-ready PNG + HTML charts."""
    _not_implemented("report charts")


# ---------- export ----------


@main.group()
def export() -> None:
    """Dataset export commands. (WP-12)"""


@export.command("dataset")
@click.option("--output-dir", type=click.Path(file_okay=False, path_type=Path), required=True)
def export_dataset(output_dir: Path) -> None:
    """Write the publishable CC-BY-4.0 dataset (parquet/csv/jsonl + manifest)."""
    _not_implemented("export dataset")


@export.command("raw")
@click.option("--output-file", type=click.Path(dir_okay=False, path_type=Path), required=True)
def export_raw(output_file: Path) -> None:
    """Write a reproducible-builds-friendly archive of all raw_json columns."""
    _not_implemented("export raw")


# ---------- top-level ----------


@main.command()
def health() -> None:
    """Report last-successful-collection per source. (WP-14)"""
    _not_implemented("health")


@main.command()
def version() -> None:
    """Print the CADENCE version."""
    console.print(__version__)


if __name__ == "__main__":  # pragma: no cover
    main()
