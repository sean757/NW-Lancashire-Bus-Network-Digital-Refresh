# Database Snapshot

This directory holds a compressed PostgreSQL dump (`transport_db.sql.gz`) used as a
fallback when the live data API (`transport.scc.lancs.ac.uk`) is unavailable.

## Creating a snapshot

With the database running and populated:

```bash
bash scripts/snapshot_db.sh          # local dev (localhost)
bash scripts/snapshot_db.sh db       # inside Docker container
```

## Restoring a snapshot

```bash
bash scripts/restore_db.sh           # local dev (localhost)
bash scripts/restore_db.sh db        # inside Docker container
```

The Docker workflow (`docker-start.sh`) automatically restores from this snapshot
when `SKIP_INGEST=true` (the default).
