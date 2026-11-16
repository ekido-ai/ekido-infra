"""Copies the Ekido services' data from the old single SQLite file into their Postgres schemas, once.

  python sqlite-to-postgres.py --sqlite /path/jobs.db --check   # read-only: tables, columns, row counts, problems
  python sqlite-to-postgres.py --sqlite /path/jobs.db --copy    # check first; write only if there are no problems

Coordinator tables go to DATABASE_URL (the coordinator's user and schema); account tables, if the file has them, go
to PLATFORM_DATABASE_URL. Both schemas must already be migrated (postgres.sh) and the services stopped.
- Tables are copied parents first, one transaction per service; a target table must be empty, so a second run can't
  duplicate rows.
- Columns only in the model get their defaults. A column only in SQLite, or a table no service owns, is a problem:
  that data would be lost, so nothing is written.
- Integer id sequences are moved past the copied ids.
Reads the SQLite file read-only and never prints row contents.
"""

from __future__ import annotations

import argparse
import os
import sys

from sqlalchemy import Integer, create_engine, func, inspect, select, text


def services():
    from ekido_coordinator.models import Base as CoordinatorBase

    out = [("coordinator", CoordinatorBase.metadata, "DATABASE_URL")]
    try:
        from ekido_platform.accounts import Base as PlatformBase

        out.append(("platform", PlatformBase.metadata, "PLATFORM_DATABASE_URL"))
    except ImportError:
        print("note: ekido_platform not importable; account tables would count as unowned")
    return out


def survey(src):
    """Returns (plan, problems): plan is [(service, env var, [(table, columns, rows)])]."""
    src_tables = set(inspect(src).get_table_names())
    covered: set[str] = set()
    plan, problems = [], 0
    for name, metadata, env in services():
        print(f"== {name}")
        items = []
        for table in metadata.sorted_tables:
            if table.name not in src_tables:
                print(f"  {table.name}: not in SQLite (stays empty)")
                continue
            covered.add(table.name)
            src_cols = {c["name"] for c in inspect(src).get_columns(table.name)}
            model_cols = {c.name for c in table.columns}
            only_sqlite, only_model = sorted(src_cols - model_cols), sorted(model_cols - src_cols)
            with src.connect() as s:
                n = s.execute(text(f'SELECT count(*) FROM "{table.name}"')).scalar_one()
            note = ""
            if only_model:
                note += f"; model-only columns get defaults: {only_model}"
            if only_sqlite:
                note += f"; PROBLEM, SQLite-only columns would be lost: {only_sqlite}"
                problems += 1
            print(f"  {table.name}: {n} rows{note}")
            items.append((table, [c for c in table.columns if c.name in src_cols], n))
        plan.append((name, env, items))
    leftover = sorted(src_tables - covered - {"alembic_version", "sqlite_sequence"})
    if leftover:
        print(f"== PROBLEM: tables no service owns: {leftover}")
        problems += 1
    return plan, problems


def copy(src, plan) -> None:
    for name, env, items in plan:
        url = os.environ.get(env)
        if not url:
            raise SystemExit(f"{env} is not set for {name}")
        with create_engine(url).begin() as d:  # one transaction per service: all of it or none of it
            for table, cols, n in items:
                existing = d.execute(select(func.count()).select_from(table)).scalar_one()
                if existing:
                    raise SystemExit(f"{table.name}: target already has {existing} rows; nothing written for {name}")
                with src.connect() as s:
                    rows = [dict(r._mapping) for r in s.execute(select(*cols))]
                for i in range(0, len(rows), 500):
                    d.execute(table.insert(), rows[i : i + 500])
                copied = d.execute(select(func.count()).select_from(table)).scalar_one()
                if copied != n:
                    raise SystemExit(f"{table.name}: {copied} of {n} rows; rolled back")
                for pk in table.primary_key.columns:
                    if n and isinstance(pk.type, Integer) and pk.autoincrement in (True, "auto"):
                        d.execute(text(f"SELECT setval(pg_get_serial_sequence('{table.name}', '{pk.name}'), "
                                       f"(SELECT max({pk.name}) FROM {table.name}))"))
                print(f"  {name}.{table.name}: copied {n}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sqlite", required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--copy", action="store_true")
    args = parser.parse_args()
    src = create_engine(f"sqlite:///file:{args.sqlite}?mode=ro&uri=true")
    plan, problems = survey(src)
    if problems:
        print(f"RESULT: {problems} problem(s); nothing written")
        return 1
    if args.copy:
        copy(src, plan)
    print("RESULT: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
