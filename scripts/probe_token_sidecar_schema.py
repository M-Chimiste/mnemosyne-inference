"""One-shot schema probe for the token-sidecar Postgres database.

Reads `TOKEN_SIDECAR_POSTGRES_DSN` from env, connects with the writer role,
and prints tables + columns + constraints + indexes in the user's default
schema. Use the output to shape the INSERT in pg_writer.py.

Run on the dev host (not inside the container):

    TOKEN_SIDECAR_POSTGRES_DSN=postgresql://... \
        .venv/bin/python scripts/probe_token_sidecar_schema.py

The script is intentionally read-only: no DDL, no INSERT, no SELECT on user
data. Safe to run repeatedly.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

import psycopg


def main() -> int:
    dsn = os.environ.get("TOKEN_SIDECAR_POSTGRES_DSN", "").strip()
    if not dsn:
        print("TOKEN_SIDECAR_POSTGRES_DSN is not set", file=sys.stderr)
        return 2

    with psycopg.connect(dsn, autocommit=True, connect_timeout=5) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT current_database(), current_user, version()")
            db, user, version = cur.fetchone()
            print(f"# Connected to: db={db} user={user}")
            print(f"# Server: {version}")
            print()

            cur.execute("SELECT current_schemas(false)")
            (schemas,) = cur.fetchone()
            print(f"# Default schemas (excluding implicit): {schemas}")
            print()

            cur.execute(
                """
                SELECT table_schema, table_name
                  FROM information_schema.tables
                 WHERE table_schema = ANY(%s)
                   AND table_type = 'BASE TABLE'
                 ORDER BY table_schema, table_name
                """,
                (list(schemas),),
            )
            tables = cur.fetchall()
            if not tables:
                print("# (no tables visible to this role)")
                return 0

            print(f"# Tables visible to this role ({len(tables)}):")
            for schema, name in tables:
                print(f"  - {schema}.{name}")
            print()

            dump: dict[str, Any] = {"database": db, "user": user, "tables": []}

            for schema, name in tables:
                print(f"== {schema}.{name} ==")
                cur.execute(
                    """
                    SELECT column_name, data_type, udt_name, is_nullable,
                           column_default, character_maximum_length
                      FROM information_schema.columns
                     WHERE table_schema = %s AND table_name = %s
                     ORDER BY ordinal_position
                    """,
                    (schema, name),
                )
                cols = cur.fetchall()
                col_dump = []
                for col_name, dtype, udt, nullable, default, maxlen in cols:
                    flags = []
                    if nullable == "NO":
                        flags.append("NOT NULL")
                    if default is not None:
                        flags.append(f"DEFAULT {default}")
                    type_str = dtype
                    if dtype == "USER-DEFINED" or dtype == "ARRAY":
                        type_str = f"{dtype} ({udt})"
                    if maxlen is not None:
                        type_str = f"{type_str}({maxlen})"
                    flag_str = "  " + " ".join(flags) if flags else ""
                    print(f"  {col_name:<28} {type_str:<28}{flag_str}")
                    col_dump.append(
                        {
                            "name": col_name,
                            "type": dtype,
                            "udt_name": udt,
                            "nullable": nullable == "YES",
                            "default": default,
                            "max_length": maxlen,
                        }
                    )

                cur.execute(
                    """
                    SELECT tc.constraint_type, tc.constraint_name,
                           string_agg(kcu.column_name, ',' ORDER BY kcu.ordinal_position)
                      FROM information_schema.table_constraints tc
                      JOIN information_schema.key_column_usage kcu
                        ON tc.constraint_name = kcu.constraint_name
                       AND tc.table_schema = kcu.table_schema
                     WHERE tc.table_schema = %s AND tc.table_name = %s
                       AND tc.constraint_type IN ('PRIMARY KEY', 'UNIQUE')
                     GROUP BY tc.constraint_type, tc.constraint_name
                     ORDER BY tc.constraint_type
                    """,
                    (schema, name),
                )
                cons = cur.fetchall()
                con_dump = []
                if cons:
                    print("  constraints:")
                    for ctype, cname, cols_csv in cons:
                        print(f"    {ctype:<12} {cname}  ({cols_csv})")
                        con_dump.append(
                            {"type": ctype, "name": cname, "columns": cols_csv.split(",")}
                        )

                cur.execute(
                    "SELECT indexname, indexdef FROM pg_indexes "
                    "WHERE schemaname = %s AND tablename = %s ORDER BY indexname",
                    (schema, name),
                )
                idx = cur.fetchall()
                idx_dump = []
                if idx:
                    print("  indexes:")
                    for iname, idef in idx:
                        print(f"    {iname}: {idef}")
                        idx_dump.append({"name": iname, "def": idef})

                print()
                dump["tables"].append(
                    {
                        "schema": schema,
                        "name": name,
                        "columns": col_dump,
                        "constraints": con_dump,
                        "indexes": idx_dump,
                    }
                )

            print("# ---- JSON dump ----")
            print(json.dumps(dump, indent=2, default=str))

    return 0


if __name__ == "__main__":
    sys.exit(main())
