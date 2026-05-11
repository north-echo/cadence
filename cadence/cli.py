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
@click.option(
    "--rhsa",
    "rhsa_ids",
    type=str,
    multiple=True,
    help="Collect specific RHSA-ID(s); may be repeated.",
)
@click.option("--all-known", is_flag=True, help="Collect CSAF for every known RHSA.")
@click.pass_obj
def collect_csaf(
    settings: Settings, rhsa_ids: tuple[str, ...], all_known: bool
) -> None:
    """Collect CSAF/VEX documents and persist VEX statements."""
    from cadence.collectors.csaf import CSAFCollector

    if not rhsa_ids and not all_known:
        err_console.print(
            "[red]error[/red]: pass --rhsa RHSA-ID (repeatable) or --all-known"
        )
        sys.exit(2)

    async def run() -> None:
        async with CSAFCollector(settings, settings.db_path) as collector:
            result = await collector.collect(
                rhsa_ids=list(rhsa_ids) or None, all_known=all_known
            )
            console.print(
                f"[green]csaf[/green]: {result.records} RHSA(s) updated "
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


@collect.command("repodata")
@click.option(
    "--repos",
    type=str,
    help="Comma-separated repo IDs (default: every UBI 8/9/10 baseos/appstream/"
    "codeready-builder x x86_64/aarch64).",
)
@click.pass_obj
def collect_repodata(settings: Settings, repos: str | None) -> None:
    """Collect cdn-ubi.redhat.com repodata. Forward-only."""
    from cadence.collectors.repodata import RepoDataCollector

    repo_list = [r.strip() for r in repos.split(",")] if repos else None

    async def run() -> None:
        async with RepoDataCollector(settings, settings.db_path) as collector:
            result = await collector.collect(repos=repo_list)
            console.print(
                f"[green]repodata[/green]: {result.records} new observation(s) "
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


@collect.command("catalog")
@click.option(
    "--repos",
    type=str,
    help="Comma-separated REPO (e.g. ubi9/ubi). Defaults to every catalog-source "
    "repo in cadence/targets.py.",
)
@click.option(
    "--since",
    type=str,
    help="ISO date (YYYY-MM-DD). Restricts to images with creation_date >= this. "
    "Default: full historical backfill.",
)
@click.option(
    "--arches",
    type=str,
    default="x86_64,aarch64",
    show_default=True,
    help="Comma-separated kernel arches.",
)
@click.pass_obj
def collect_catalog(
    settings: Settings, repos: str | None, since: str | None, arches: str
) -> None:
    """Collect Red Hat Container Catalog images + RPM manifests."""
    from cadence.collectors.catalog import CatalogCollector

    repo_list = [r.strip() for r in repos.split(",")] if repos else None
    arch_tuple = tuple(a.strip() for a in arches.split(","))

    async def run() -> None:
        async with CatalogCollector(settings, settings.db_path) as collector:
            result = await collector.collect(
                repos=repo_list, arches=arch_tuple, since=since
            )
            console.print(
                f"[green]catalog[/green]: {result.records} image(s) "
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


@collect.command("quay")
@click.option(
    "--repos",
    type=str,
    help="Comma-separated NS/NAME. Defaults to every Quay-source repo "
    "in cadence/targets.py.",
)
@click.pass_obj
def collect_quay(settings: Settings, repos: str | None) -> None:
    """Collect Quay.io tag history + OCI manifests."""
    from cadence.collectors.quay import QuayCollector

    repo_list = [r.strip() for r in repos.split(",")] if repos else None

    async def run() -> None:
        async with QuayCollector(settings, settings.db_path) as collector:
            result = await collector.collect(repos=repo_list)
            console.print(
                f"[green]quay[/green]: {result.records} image row(s) "
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


# ---------- verify ----------


@main.group()
def verify() -> None:
    """Registry verification commands. (WP-08)"""


def _print_verification(results: list) -> None:
    from cadence.collectors.registry import summarize

    counts = summarize(results)
    for r in results:
        if r.status == "ok":
            console.print(
                f"  [green]ok[/green]       {r.reference}  ({r.image_id[:12]})"
            )
        elif r.status == "drift":
            console.print(
                f"  [yellow]drift[/yellow]    {r.reference}  ({r.image_id[:12]})"
            )
            for d in r.discrepancies:
                err_console.print(f"           {d}")
        elif r.status == "not_in_database":
            err_console.print(f"  [yellow]missing[/yellow]  {r.reference} (not in db)")
        elif r.status == "skopeo_unavailable":
            err_console.print(
                f"  [yellow]skipped[/yellow]  {r.reference} (skopeo not installed)"
            )
        else:  # error
            err_console.print(
                f"  [red]error[/red]    {r.reference}: {r.error or 'unknown'}"
            )
    pretty = ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
    console.print(f"[green]verify[/green]: {pretty or 'no results'}")


@verify.command("image")
@click.argument("ref")
@click.pass_obj
def verify_image_cmd(settings: Settings, ref: str) -> None:
    """Cross-validate a single REPO:TAG against the database."""
    from cadence.collectors.registry import verify_image

    if ":" not in ref:
        err_console.print("[red]error[/red]: REF must be REPO:TAG")
        sys.exit(2)
    repository, tag = ref.rsplit(":", 1)

    with connect(settings.db_path) as conn:
        results = verify_image(conn, repository, tag)
    _print_verification(results)


@verify.command("random")
@click.option("--sample", "sample", type=int, default=10, show_default=True)
@click.pass_obj
def verify_random_cmd(settings: Settings, sample: int) -> None:
    """Cross-validate N randomly selected images."""
    from cadence.collectors.registry import verify_random_sample

    with connect(settings.db_path) as conn:
        results = verify_random_sample(conn, sample)
    _print_verification(results)


# ---------- analyze ----------


@main.group()
def analyze() -> None:
    """Analysis commands."""


@analyze.command("reconstruct")
@click.option(
    "--methodology-version",
    type=str,
    default=None,
    help="Tag for this run (default: 'v1'). Multiple versions coexist.",
)
@click.pass_obj
def analyze_reconstruct(
    settings: Settings, methodology_version: str | None
) -> None:
    """Reconstruct gap_measurement + rebuild_interval from raw data."""
    from cadence.analysis.reconstruct import DEFAULT_METHODOLOGY_VERSION, reconstruct

    version = methodology_version or DEFAULT_METHODOLOGY_VERSION

    with connect(settings.db_path) as conn:
        result = reconstruct(conn, methodology_version=version)

    rate = result.cross_check_match_rate
    rate_str = f"{rate * 100:.1f}%" if rate is not None else "n/a"
    console.print(
        f"[green]reconstruct[/green] (methodology={version}): "
        f"{result.gap_rows_written} gap row(s), "
        f"{result.intervals_written} interval(s), "
        f"{result.not_affected_skipped} VEX not_affected skipped, "
        f"cross-check {result.cross_check_matched}/{result.cross_check_total} "
        f"({rate_str}) in {result.duration_seconds:.1f}s"
    )


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
