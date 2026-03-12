#!/usr/bin/env python3
"""Query timetable rows for a given operator code (default: SCCU).

Usage
-----
    python scripts/query_timetables.py              # default operator SCCU
    python scripts/query_timetables.py BLAC         # override operator
    python scripts/query_timetables.py --limit 50   # cap row count

Environment variables (same as the main app):
    DB_HOST, DB_PORT, DB_USER, DB_PASSWORD, DB_NAME
"""

import argparse
import os
import sys

import psycopg2
from psycopg2.extras import RealDictCursor

# ---------------------------------------------------------------------------
# Database defaults (mirror app/config.py)
# ---------------------------------------------------------------------------
DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = os.getenv("DB_PORT", "5432")
DB_USER = os.getenv("DB_USER", "transport")
DB_PASSWORD = os.getenv("DB_PASSWORD", "transport_dev")
DB_NAME = os.getenv("DB_NAME", "transport_db")

QUERY = """
SELECT t.id,
       r.route_name,
       r.operator,
       r.description   AS route_description,
       t.trip_id,
       t.stop_id,
       s.stop_name,
       t.arrival_time,
       t.departure_time,
       t.stop_sequence,
       t.direction,
       t.days_of_week,
       t.valid_from,
       t.valid_until
  FROM timetables t
  JOIN routes r ON r.route_id = t.route_id
  LEFT JOIN stops s ON s.stop_id = t.stop_id
 WHERE r.operator = %s
 ORDER BY r.route_name, t.trip_id, t.stop_sequence
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Query timetable rows by operator code."
    )
    parser.add_argument(
        "operator",
        nargs="?",
        default="SCCU",
        help="Operator code to filter by (default: SCCU)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum number of rows to return",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    try:
        conn = psycopg2.connect(
            host=DB_HOST,
            port=DB_PORT,
            user=DB_USER,
            password=DB_PASSWORD,
            dbname=DB_NAME,
        )
    except psycopg2.OperationalError as exc:
        print(f"ERROR: could not connect to database: {exc}", file=sys.stderr)
        sys.exit(1)

    query = QUERY
    if args.limit:
        query += f" LIMIT {int(args.limit)}"

    with conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(query, (args.operator,))
        rows = cur.fetchall()

    if not rows:
        print(f"No timetable rows found for operator '{args.operator}'.")
        sys.exit(0)

    # Pretty-print as a table
    columns = list(rows[0].keys())
    col_widths = {c: len(c) for c in columns}
    for row in rows:
        for c in columns:
            col_widths[c] = max(col_widths[c], len(
                str(row[c] if row[c] is not None else "")))

    header = " | ".join(c.ljust(col_widths[c]) for c in columns)
    separator = "-+-".join("-" * col_widths[c] for c in columns)

    print(
        f"\nTimetable results for operator '{args.operator}' ({len(rows)} rows)\n")
    print(header)
    print(separator)
    for row in rows:
        print(" | ".join(
            str(row[c] if row[c] is not None else "").ljust(col_widths[c])
            for c in columns
        ))

    conn.close()


if __name__ == "__main__":
    main()
