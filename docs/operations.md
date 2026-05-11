# CADENCE Operations

This document covers running CADENCE as a continuous collection service
on a Linux host — its target deployment shape. It pairs with
[`reproducing-findings.md`](reproducing-findings.md) (one-shot
reproduction) and [`methodology.md`](methodology.md) (the analysis
approach).

Where it matters, this doc is written against the production target
described in `CADENCE-SPEC.md`: an OptiPlex 7060 Micro (i7-8700T, 16 GB
DDR4, Fedora Server 42) cohabitating with Fluxgate. Sensible defaults on
other hosts follow the same pattern.

## Install

```bash
# As the operator's own user (NOT root). Same user that runs Fluxgate.
sudo dnf install -y python3.12 python3-rpm skopeo
pip install --user 'ne-cadence[export]'

# Sanity check
which cadence            # → ~/.local/bin/cadence
cadence --version
cadence db init
```

`pip install --user` puts the binary at `~/.local/bin/cadence`, which
is the path the shipped systemd units hard-code via `%h/.local/bin/cadence`.
If you installed via `uv` or into a different venv, edit the
`ExecStart=` lines in `systemd/user/cadence-collect-*.service` before
enabling the timers, or symlink the venv binary into
`~/.local/bin/cadence`.

## Cohabitation

CADENCE is designed to share a host with at least one other tenant
(Fluxgate, in the canonical deployment). The contract:

| Resource | CADENCE's position |
|---|---|
| Run as | Operator's user account, never root. |
| Database | `~/.local/share/cadence/cadence.db`, WAL-mode SQLite. |
| HTTP cache | `~/.cache/cadence/`. Safe to delete; the next run re-warms it. |
| systemd scope | **User-level only** (`systemctl --user …`). No system services. |
| Timer minute offsets | `:17` and `:47` past the hour, with `RandomizedDelaySec=10min`. Keeps CADENCE off conventional cron rails (`:00/:15/:30/:45`) so it doesn't fight Fluxgate-style schedulers. |
| `Nice=10`, `IOSchedulingPriority=4` | CADENCE de-prioritises itself when the host is busy. |
| `MemoryMax=1G` | Hard ceiling per service; see resource budget below. |

The shipped schedule assumes Fluxgate is the *only* other periodic
workload. If a third tenant lands, adjust the minute offsets to taste
and document the new layout here.

## The schedule

`systemctl --user list-timers` after enabling the units should show:

```
NEXT                        LEFT       LAST  UNIT
… 02:17                    …          n/a   cadence-collect-rhsa.timer
… 02:47                    …          n/a   cadence-collect-csaf.timer
… 00:17 / 04:17 / …        …          n/a   cadence-collect-repodata.timer
… 03:47 / 15:47            …          n/a   cadence-collect-catalog.timer
… 09:17 / 21:17            …          n/a   cadence-collect-quay.timer
```

| Source | Cadence | Why |
|---|---|---|
| `cadence-collect-rhsa.timer` | every 4h at `:17` | New RHSAs land throughout the day; 4h matches Gap A's bound. |
| `cadence-collect-csaf.timer` | every 4h at `:47` | Offset 30min after RHSA so each CSAF run sees fresh RHSA data. |
| `cadence-collect-repodata.timer` | every 4h at `:17` (shifted 2h) | Forward-only Gap A floor; tighter cadence buys precision at a host cost. |
| `cadence-collect-catalog.timer` | every 12h at `:47` | Incremental (`--since 36h ago`). Full backfill is manual. |
| `cadence-collect-quay.timer` | every 12h at `:17` | Quay tag turnover is slow; 12h is plenty. |

## Install the timers

```bash
mkdir -p ~/.config/systemd/user
cp systemd/user/cadence-collect-*.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload

# Enable + start all five
for svc in rhsa csaf repodata catalog quay; do
    systemctl --user enable --now cadence-collect-$svc.timer
done

# Verify
systemctl --user list-timers cadence-collect-\*
journalctl --user -u cadence-collect-rhsa.service -n 50
```

The first run of `cadence-collect-catalog` is *incremental*. To prime
the database with the full historical UBI backfill, run that step
manually before enabling the timer:

```bash
cadence collect catalog       # ~3h at default rate limits
```

## Health and metrics

```bash
# Per-source freshness; exits non-zero if any source has been silent
# for more than 2x its expected cadence.
cadence health

# Optional Prometheus scrape endpoint. Loopback-only by default;
# never bind a public interface for the unauthenticated endpoint.
cadence metrics serve --bind 127.0.0.1 --port 9101
# Then:
curl -s http://127.0.0.1:9101/metrics
```

Useful alerts to drive off the metrics:

* `cadence_collection_ok{source=…} == 0` for more than 1h → a collector
  has gone silent.
* `cadence_database_bytes > 4 × 1024^3` → approaching the soft-warn
  threshold (see resource budget).

The metrics endpoint reads the database on every scrape; there are no
in-memory counters. Restart it freely.

## Resource budget

| Resource | Soft warn | Hard cap | What to do |
|---|---|---|---|
| Disk (`~/.local/share/cadence/`) | 4 GB | 5 GB | Run `sqlite3 cadence.db 'VACUUM;'` or rotate the raw-archive exports. |
| Memory (per service) | n/a | `MemoryMax=1G` | The collector should not exceed it; the analysis re-runner chunks via SQLite cursors. If you OOM, file an issue with the database size + the offending command. |
| CPU | n/a | `Nice=10` | Collection is HTTP-bound, not CPU-bound. Analysis bursts to single-digit minutes on the full dataset. |
| Network | n/a | 1 req/sec per upstream host | Configurable via `CADENCE_RATE_LIMIT_PER_HOST` env var or `[rate_limit_per_host]` config. Be polite. |

The disk soft-warn is operator-tracked (no automatic enforcement in
v1); a sensible monitor:

```bash
# Warn at 4 GB
du -sh ~/.local/share/cadence/
```

## Backup

```bash
# Nightly snapshot — uses sqlite3 .backup so the live database can stay
# open. Drop into ~/cadence-backups/ (or wherever fits your storage layout).
sqlite3 ~/.local/share/cadence/cadence.db ".backup ~/cadence-backups/cadence-$(date +%Y%m%d).db"

# Weekly: archive a published dataset bundle
mkdir -p ~/cadence-backups/dataset-$(date +%Y%m%d)
cadence export dataset --output-dir ~/cadence-backups/dataset-$(date +%Y%m%d)
cadence export raw     --output-file ~/cadence-backups/dataset-$(date +%Y%m%d)/raw.tar.zst
```

`cadence export raw` is byte-for-byte deterministic for the same
database state, so two weekly archives of the same database produce
identical files — handy for change-detection.

## Pause and resume

The collection workload is purely additive; pausing it is safe at any
point.

```bash
# Pause all five timers (Fluxgate doing heavy work, host reboot, …)
for svc in rhsa csaf repodata catalog quay; do
    systemctl --user stop cadence-collect-$svc.timer
done

# Resume
for svc in rhsa csaf repodata catalog quay; do
    systemctl --user start cadence-collect-$svc.timer
done

# Or, take the whole stack down for upgrades
systemctl --user disable --now 'cadence-collect-*.timer'
```

Because every timer specifies `Persistent=true`, a missed run after a
host reboot fires once at boot rather than waiting for the next
scheduled tick.

## Troubleshooting

| Symptom | First check | Then |
|---|---|---|
| `cadence health` says `stale` | `journalctl --user -u cadence-collect-<source>.service --since '1 day ago'` | Look for non-zero exits or HTTP errors. |
| Disk usage climbing | `du -sh ~/.cache/cadence/` | The cache is safe to delete: `rm -rf ~/.cache/cadence/`. |
| `sqlite3 cadence.db 'PRAGMA integrity_check;'` returns anything but `ok` | Stop timers; restore from the most recent backup. | Investigate before re-enabling timers — the WAL log may be corrupt. |
| `cadence collect catalog` slower than expected | Check the catalog API's response times; the spec's ~3h estimate assumes the live API is healthy. | If transient, retry — the cache plus collection-run recording means a re-run resumes where the last one finished. |
| Metrics endpoint returns 500s | `journalctl --user -u cadence-metrics.service` (or your wrapper) | Most likely the database file is missing or unreadable; verify `CADENCE_DB_PATH`. |

## Upgrade

```bash
pip install --user --upgrade ne-cadence
cadence db migrate         # apply any pending migrations
systemctl --user daemon-reload
```

Migrations are idempotent and run inside a transaction. Existing data
is preserved.
