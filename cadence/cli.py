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
    record_collection_run,
)

console = Console()
err_console = Console(stderr=True)


def _not_implemented(name: str) -> None:
    err_console.print(
        f"[yellow]cadence {name}[/yellow]: not implemented yet (stub from WP-01)."
    )
    sys.exit(2)


def _record_run(settings: Settings, source: str, result) -> None:
    """Append a `collection_run` row for one collector invocation."""
    with connect(settings.db_path) as conn:
        record_collection_run(
            conn,
            source=source,
            started_at=result.started_at.isoformat(),
            completed_at=result.completed_at.isoformat(),
            records=result.records,
            errors=result.errors,
        )


def _finish_collect_run(result, *, source: str) -> None:
    """Render per-record errors as warnings, then decide the CLI exit code.

    Per-record errors are reported but NOT fatal: the collector already
    isolated them, so a run with 5 errors out of 5000 records is healthy.
    We exit non-zero only when the run was effectively useless — zero
    records AND at least one error. (If the collector itself raised, it
    has already escaped this code path.)
    """
    if result.errors:
        for msg in result.errors[:10]:
            err_console.print(f"  [yellow]![/yellow] {msg}")
        if len(result.errors) > 10:
            err_console.print(
                f"  … and {len(result.errors) - 10} more"
            )
    if result.records == 0 and result.errors:
        err_console.print(
            f"[red]{source}[/red]: 0 records persisted, "
            f"{len(result.errors)} error(s) — treating as failure."
        )
        sys.exit(1)


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
            _record_run(settings, "rhsa", result)
            console.print(
                f"[green]rhsa[/green]: {result.records} RHSA(s) persisted "
                f"in {result.duration_seconds:.1f}s "
                f"({len(result.errors)} error(s))"
            )
            _finish_collect_run(result, source="rhsa")

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
            _record_run(settings, "csaf", result)
            console.print(
                f"[green]csaf[/green]: {result.records} RHSA(s) updated "
                f"in {result.duration_seconds:.1f}s "
                f"({len(result.errors)} error(s))"
            )
            _finish_collect_run(result, source="csaf")

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
            _record_run(settings, "repodata", result)
            console.print(
                f"[green]repodata[/green]: {result.records} new observation(s) "
                f"in {result.duration_seconds:.1f}s "
                f"({len(result.errors)} error(s))"
            )
            _finish_collect_run(result, source="repodata")

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
            _record_run(settings, "catalog", result)
            console.print(
                f"[green]catalog[/green]: {result.records} image(s) "
                f"in {result.duration_seconds:.1f}s "
                f"({len(result.errors)} error(s))"
            )
            _finish_collect_run(result, source="catalog")

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
            _record_run(settings, "quay", result)
            console.print(
                f"[green]quay[/green]: {result.records} image row(s) "
                f"in {result.duration_seconds:.1f}s "
                f"({len(result.errors)} error(s))"
            )
            _finish_collect_run(result, source="quay")

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
@click.option(
    "--skip-quay-targets/--include-quay-targets",
    default=False,
    show_default=True,
    help="Skip emitting gap_measurement rows for Quay-source tracked repos. "
    "Quay tracked repos always emit NULL image-side gaps in v1 (no "
    "container_image_rpm rows); this trims the dataset without losing "
    "analytical content. Default is False (spec-compliant: emit everything).",
)
@click.pass_obj
def analyze_reconstruct(
    settings: Settings,
    methodology_version: str | None,
    skip_quay_targets: bool,
) -> None:
    """Reconstruct gap_measurement + rebuild_interval from raw data."""
    from cadence.analysis.reconstruct import DEFAULT_METHODOLOGY_VERSION, reconstruct

    version = methodology_version or DEFAULT_METHODOLOGY_VERSION

    with connect(settings.db_path) as conn:
        result = reconstruct(
            conn,
            methodology_version=version,
            skip_quay_targets=skip_quay_targets,
        )

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


def _render_distribution(
    stats: list, *, facet_label: str, value_unit: str, fmt: str
) -> None:
    """Print a list of DistributionStats as Rich table, JSON, or CSV."""
    import csv

    if fmt == "json":
        console.print_json(data=[s.as_dict() for s in stats])
        return
    if fmt == "csv":
        import io

        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow([
            "facet", "count", "mean", "stddev",
            "median", "p25", "p75", "p90", "p95", "p99",
            "low_n_warning",
        ])
        for s in stats:
            writer.writerow([
                s.facet, s.count,
                f"{s.mean:.2f}" if s.mean is not None else "",
                f"{s.stddev:.2f}" if s.stddev is not None else "",
                f"{s.median:.2f}" if s.median is not None else "",
                f"{s.p25:.2f}" if s.p25 is not None else "",
                f"{s.p75:.2f}" if s.p75 is not None else "",
                f"{s.p90:.2f}" if s.p90 is not None else "",
                f"{s.p95:.2f}" if s.p95 is not None else "",
                f"{s.p99:.2f}" if s.p99 is not None else "",
                "yes" if s.low_n_warning else "",
            ])
        click.echo(buf.getvalue().rstrip("\n"))
        return

    # default: Rich table
    from rich.table import Table

    table = Table(show_header=True, header_style="bold")
    table.add_column(facet_label)
    table.add_column("N", justify="right")
    for header in ("median", "p25", "p75", "p90", "p95", "p99", "mean", "stddev"):
        table.add_column(f"{header} ({value_unit})", justify="right")
    for s in stats:
        warn = " [yellow]⚠[/yellow]" if s.low_n_warning else ""
        def fmt_v(v: float | None) -> str:
            if v is None:
                return "—"
            return f"{v:,.0f}"
        table.add_row(
            f"{s.facet}{warn}", str(s.count),
            fmt_v(s.median), fmt_v(s.p25), fmt_v(s.p75),
            fmt_v(s.p90), fmt_v(s.p95), fmt_v(s.p99),
            fmt_v(s.mean), fmt_v(s.stddev),
        )
    console.print(table)
    if any(s.low_n_warning for s in stats):
        err_console.print(
            "[yellow]⚠[/yellow] one or more slices have N<30; "
            "percentiles are unreliable at this sample size."
        )


@analyze.command("gaps")
@click.option(
    "--gap",
    type=click.Choice(["A", "B", "C"]),
    default="C",
    show_default=True,
    help="Which gap to summarize.",
)
@click.option("--slice-by", "slice_by", type=str, help="Facet to slice by.")
@click.option("--tier", type=str, help="Restrict to a single tier.")
@click.option(
    "--top",
    "top_n",
    type=int,
    default=None,
    help="When slicing by a high-cardinality facet, keep only the N "
    "facets with the most observations.",
)
@click.option(
    "--methodology-version",
    type=str,
    default=None,
    help="Methodology version tag (default: 'v1').",
)
@click.option(
    "--format",
    "fmt",
    type=click.Choice(["table", "json", "csv"]),
    default="table",
    show_default=True,
)
@click.pass_obj
def analyze_gaps(
    settings: Settings,
    gap: str,
    slice_by: str | None,
    tier: str | None,
    top_n: int | None,
    methodology_version: str | None,
    fmt: str,
) -> None:
    """Compute gap distributions, optionally sliced by facet."""
    from cadence.analysis.gaps import gap_distribution
    from cadence.analysis.reconstruct import DEFAULT_METHODOLOGY_VERSION

    version = methodology_version or DEFAULT_METHODOLOGY_VERSION
    with connect(settings.db_path) as conn:
        stats = gap_distribution(
            conn,
            gap=gap,  # type: ignore[arg-type]
            slice_by=slice_by,
            methodology_version=version,
            tier=tier,
            top_n=top_n,
        )
    _render_distribution(
        stats,
        facet_label=slice_by or f"Gap {gap} (overall)",
        value_unit="s",
        fmt=fmt,
    )


@analyze.command("intervals")
@click.option("--slice-by", "slice_by", type=str, help="Facet to slice by.")
@click.option("--tier", type=str, help="Restrict to a single tier.")
@click.option("--top", "top_n", type=int, default=None,
              help="Keep top-N facets by observation count.")
@click.option(
    "--format",
    "fmt",
    type=click.Choice(["table", "json", "csv"]),
    default="table",
    show_default=True,
)
@click.pass_obj
def analyze_intervals(
    settings: Settings,
    slice_by: str | None,
    tier: str | None,
    top_n: int | None,
    fmt: str,
) -> None:
    """Compute inter-build interval distributions."""
    from cadence.analysis.intervals import interval_distribution

    with connect(settings.db_path) as conn:
        stats = interval_distribution(
            conn, slice_by=slice_by, tier=tier, top_n=top_n
        )
    _render_distribution(
        stats,
        facet_label=slice_by or "Inter-build interval (overall)",
        value_unit="s",
        fmt=fmt,
    )


# ---------- report ----------


@main.group()
def report() -> None:
    """Report generation commands. (WP-11)"""


@report.command("summary")
@click.option(
    "--methodology-version", type=str, default=None,
    help="Methodology version tag (default: 'v1').",
)
@click.pass_obj
def report_summary(settings: Settings, methodology_version: str | None) -> None:
    """Print a Rich-formatted summary of all findings."""
    from cadence.analysis.reconstruct import DEFAULT_METHODOLOGY_VERSION
    from cadence.reports.summary import render_summary

    version = methodology_version or DEFAULT_METHODOLOGY_VERSION
    with connect(settings.db_path) as conn:
        render_summary(conn, console, methodology_version=version)


@report.command("markdown")
@click.option(
    "--output", type=click.Path(dir_okay=False, path_type=Path), required=True
)
@click.option(
    "--methodology-version", type=str, default=None,
    help="Methodology version tag (default: 'v1').",
)
@click.option(
    "--charts-dir",
    type=str, default="charts", show_default=True,
    help="Relative directory for chart images referenced from the report.",
)
@click.pass_obj
def report_markdown_cmd(
    settings: Settings,
    output: Path,
    methodology_version: str | None,
    charts_dir: str,
) -> None:
    """Write a comprehensive Markdown report."""
    from cadence.analysis.reconstruct import DEFAULT_METHODOLOGY_VERSION
    from cadence.reports.markdown import render_markdown

    version = methodology_version or DEFAULT_METHODOLOGY_VERSION
    with connect(settings.db_path) as conn:
        render_markdown(
            conn, output,
            methodology_version=version, charts_dir_relative=charts_dir,
        )
    console.print(f"[green]markdown[/green]: wrote {output}")


@report.command("charts")
@click.option(
    "--output-dir",
    type=click.Path(file_okay=False, path_type=Path), required=True
)
@click.option(
    "--methodology-version", type=str, default=None,
    help="Methodology version tag (default: 'v1').",
)
@click.pass_obj
def report_charts_cmd(
    settings: Settings, output_dir: Path, methodology_version: str | None
) -> None:
    """Write publication-ready PNG + HTML charts."""
    from cadence.analysis.reconstruct import DEFAULT_METHODOLOGY_VERSION
    from cadence.reports.charts import render_all

    version = methodology_version or DEFAULT_METHODOLOGY_VERSION
    with connect(settings.db_path) as conn:
        results = render_all(conn, output_dir, methodology_version=version)
    n_empty = sum(1 for r in results if r.skipped_empty)
    console.print(
        f"[green]charts[/green]: wrote {len(results)} chart(s) to "
        f"{output_dir} ({n_empty} empty placeholder(s))"
    )


# ---------- export ----------


@main.group()
def export() -> None:
    """Dataset export commands. (WP-12)"""


@export.command("dataset")
@click.option(
    "--output-dir",
    type=click.Path(file_okay=False, path_type=Path), required=True,
)
@click.option(
    "--methodology-version", type=str, default=None,
    help="Methodology version tag (default: 'v1').",
)
@click.pass_obj
def export_dataset_cmd(
    settings: Settings, output_dir: Path, methodology_version: str | None
) -> None:
    """Write the publishable CC-BY-4.0 dataset (parquet/csv/jsonl + manifest)."""
    from cadence.analysis.export import ExportDependencyMissing, export_dataset
    from cadence.analysis.reconstruct import DEFAULT_METHODOLOGY_VERSION

    version = methodology_version or DEFAULT_METHODOLOGY_VERSION
    try:
        with connect(settings.db_path) as conn:
            manifest = export_dataset(
                conn, output_dir, methodology_version=version
            )
    except ExportDependencyMissing as exc:
        err_console.print(f"[red]error[/red]: {exc}")
        sys.exit(2)
    console.print(
        f"[green]export dataset[/green]: {manifest.row_count} row(s) "
        f"({manifest.rhsa_count} RHSA(s)), methodology=[cyan]{version}[/cyan], "
        f"to {output_dir}"
    )


@export.command("raw")
@click.option(
    "--output-file",
    type=click.Path(dir_okay=False, path_type=Path), required=True,
)
@click.pass_obj
def export_raw_cmd(settings: Settings, output_file: Path) -> None:
    """Write a reproducible-builds-friendly archive of all raw_json columns."""
    from cadence.analysis.export import ExportDependencyMissing, export_raw

    try:
        with connect(settings.db_path) as conn:
            n = export_raw(conn, output_file)
    except ExportDependencyMissing as exc:
        err_console.print(f"[red]error[/red]: {exc}")
        sys.exit(2)
    size = output_file.stat().st_size
    console.print(
        f"[green]export raw[/green]: {n} entry(ies), {size:,} bytes "
        f"→ {output_file}"
    )


# ---------- top-level ----------


@main.command()
@click.pass_obj
def health(settings: Settings) -> None:
    """Report last-successful-collection per source.

    Exits non-zero when any tracked source has been silent for more than
    twice its expected interval.
    """
    from cadence.health import health_check, render_health

    with connect(settings.db_path) as conn:
        result = health_check(conn)
    render_health(result, console)
    if not result.overall_ok:
        sys.exit(1)


@main.group()
def cache() -> None:
    """Local HTTP cache management."""


@cache.command("size")
@click.pass_obj
def cache_size_cmd(settings: Settings) -> None:
    """Report the current size of the local HTTP cache."""
    from cadence.cache import cache_size_bytes

    n = cache_size_bytes(settings.cache_dir)
    console.print(
        f"[green]cache[/green]: {n:,} bytes ({n / 1_048_576:.1f} MiB) "
        f"at {settings.cache_dir}"
    )


@cache.command("prune")
@click.option(
    "--max-bytes", type=int,
    default=None,
    help="Cap on total cache size in bytes (default: 1 GiB).",
)
@click.pass_obj
def cache_prune_cmd(settings: Settings, max_bytes: int | None) -> None:
    """Trim oldest cache files until total size <= --max-bytes.

    Intended to run nightly via ``cadence-cache-prune.timer``; safe to
    invoke ad-hoc when disk space gets tight.
    """
    from cadence.cache import DEFAULT_MAX_BYTES, prune

    cap = DEFAULT_MAX_BYTES if max_bytes is None else max_bytes
    result = prune(settings.cache_dir, max_bytes=cap)
    console.print(
        f"[green]cache prune[/green]: "
        f"{result.bytes_freed:,} bytes freed across "
        f"{result.files_removed} file(s); "
        f"now at {result.total_bytes_after:,} / {cap:,} bytes."
    )


@main.group()
def metrics() -> None:
    """Optional Prometheus-style metrics endpoint."""


@metrics.command("serve")
@click.option(
    "--bind",
    type=str,
    default="127.0.0.1",
    show_default=True,
    help="Bind address. Loopback by default; never bind a public interface "
    "for the unauthenticated endpoint.",
)
@click.option(
    "--port", type=int, default=9101, show_default=True,
)
@click.pass_obj
def metrics_serve(settings: Settings, bind: str, port: int) -> None:
    """Serve a /metrics endpoint for Prometheus scraping."""
    from cadence.metrics import serve_metrics

    serve_metrics(settings, console=console, bind=bind, port=port)


@main.command()
def version() -> None:
    """Print the CADENCE version."""
    console.print(__version__)


if __name__ == "__main__":  # pragma: no cover
    main()
