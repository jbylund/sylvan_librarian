"""Bulk upsert into any table using jsonb_array_elements + LEFT JOIN.

Accepts a list of dicts serialized to a single JSON array parameter. For each
column in each row the proposed value is resolved before the conflict clause:
  - Key present in the dict (including value None): proposed value = dict's value.
  - Key absent from the dict: proposed value = the existing row's value (NULL for new rows).

No staging table, no jsonb_populate_record.
"""

from __future__ import annotations

from typing import Any

import orjson
import psycopg
from psycopg import sql
from psycopg.rows import dict_row

_PG_TYPE: dict[str, str] = {
    "bigint": "bigint",
    "boolean": "boolean",
    "character": "text",
    "character varying": "text",
    "date": "date",
    "double precision": "float8",
    "integer": "integer",
    "json": "json",
    "jsonb": "jsonb",
    "numeric": "numeric",
    "real": "float4",
    "smallint": "smallint",
    "text": "text",
    "timestamp with time zone": "timestamptz",
    "timestamp without time zone": "timestamp",
    "uuid": "uuid",
}


def _schema_info(cursor: psycopg.Cursor, schema: str, table: str) -> tuple[list[dict], list[str]]:
    cursor.execute(
        """
        SELECT column_name, data_type, udt_name
        FROM information_schema.columns
        WHERE table_schema = %s AND table_name = %s
        ORDER BY ordinal_position
        """,
        (schema, table),
    )
    columns = cursor.fetchall()

    cursor.execute(
        """
        SELECT kcu.column_name
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
            ON tc.constraint_name = kcu.constraint_name
           AND tc.table_schema = kcu.table_schema
           AND tc.table_name = kcu.table_name
        WHERE tc.constraint_type = 'PRIMARY KEY'
          AND tc.table_schema = %s
          AND tc.table_name = %s
        ORDER BY kcu.ordinal_position
        """,
        (schema, table),
    )
    return columns, [r["column_name"] for r in cursor.fetchall()]


def _col_pg_type(col: dict) -> str:
    dt = col["data_type"]
    if dt == "USER-DEFINED":
        return col["udt_name"]
    return _PG_TYPE.get(dt, "text")


def _select_expr(col: str, pg_type: str) -> sql.Composed:
    """Build: CASE WHEN obj ? 'col' THEN <extract> ELSE e.col END AS col.

    Key present (including explicit null) → extract from JSON.
    Key absent → fall back to existing row value via LEFT JOIN alias e.
    """
    key = sql.Literal(col)
    col_id = sql.Identifier(col)

    if pg_type in ("jsonb", "json"):
        extract = sql.SQL("obj->{}").format(key)
    elif pg_type == "text":
        extract = sql.SQL("obj->>{}").format(key)
    else:
        extract = sql.SQL("(obj->>{key})::{type}").format(key=key, type=sql.SQL(pg_type))

    return sql.SQL("CASE WHEN obj ? {key} THEN {extract} ELSE e.{col} END AS {col}").format(key=key, extract=extract, col=col_id)


def _dedupe_rows(rows: list[dict[str, Any]], key_cols: list[str]) -> list[dict[str, Any]]:
    """Keep one row per conflict key; later rows overwrite earlier duplicates."""
    unique: dict[tuple[Any, ...], dict[str, Any]] = {}
    for row in rows:
        unique[tuple(row[c] for c in key_cols)] = row
    return list(unique.values())


def bulk_upsert(  # noqa: PLR0913
    conn: psycopg.Connection,
    table: str,
    rows: list[dict[str, Any]],
    schema: str = "public",
    conflict_target: list[str] | None = None,
    skip_columns: list[str] | None = None,
) -> dict[str, int]:
    """Upsert *rows* into *schema*.*table* via a single JSON array parameter.

    For each column in each row:
    - Key present in the dict (even value None): the dict's value is written.
    - Key absent from the dict: the existing row's value is preserved (NULL for new rows).

    Rows that would be unchanged are not rewritten.
    No staging table, no jsonb_populate_record.

    *skip_columns*: column names excluded from ON CONFLICT DO UPDATE SET and from the
    IS DISTINCT FROM check. Skipped columns are still inserted for new rows; on conflict
    they are left at their existing values and do not contribute to change detection.
    Useful for application-managed columns (e.g. tag fields) that are absent from the
    upstream data source and should never be overwritten by an import.

    Returns {"inserted": N, "updated": M, "unchanged": K}.
    """
    if not rows:
        return {"inserted": 0, "updated": 0, "unchanged": 0}

    with conn.cursor(row_factory=dict_row) as cur:
        schema_cols, pk_cols = _schema_info(cur, schema, table)

    pk_cols = conflict_target or pk_cols
    if not pk_cols:
        msg = f"No primary key on {schema}.{table}; pass conflict_target"
        raise ValueError(msg)

    rows = _dedupe_rows(rows, pk_cols)

    col_meta = {c["column_name"]: c for c in schema_cols}
    schema_order = [c["column_name"] for c in schema_cols]

    # Only columns present in at least one input row and known to the schema.
    present = set().union(*(r.keys() for r in rows)) & col_meta.keys()
    missing_pk = [c for c in pk_cols if c not in present]
    if missing_pk:
        msg = f"Primary key column(s) {missing_pk} absent from all input rows"
        raise ValueError(msg)

    skip = set(skip_columns or [])
    active = [c for c in schema_order if c in present]
    non_pk = [c for c in active if c not in pk_cols and c not in skip]
    pg_types = {c: _col_pg_type(col_meta[c]) for c in active}

    schema_id, table_id = sql.Identifier(schema), sql.Identifier(table)
    col_ids = [sql.Identifier(c) for c in active]
    pk_ids = [sql.Identifier(c) for c in pk_cols]

    # CASE WHEN obj ? 'col' THEN extract ELSE e.col END AS col
    select_exprs = sql.SQL(", ").join(_select_expr(c, pg_types[c]) for c in active)

    # LEFT JOIN on PK to make existing row values available to the CASE expressions
    join_cond = sql.SQL(" AND ").join(
        sql.SQL("e.{col} = (obj->>{key})::{type}").format(
            col=sql.Identifier(c),
            key=sql.Literal(c),
            type=sql.SQL(pg_types[c]),
        )
        for c in pk_cols
    )

    if non_pk:
        set_clause = sql.SQL(", ").join(sql.SQL("{c} = EXCLUDED.{c}").format(c=sql.Identifier(c)) for c in non_pk)
        # EXCLUDED already carries the resolved value, so a plain IS DISTINCT FROM suffices.
        where_clause = sql.SQL("({tbl}) IS DISTINCT FROM ({excl})").format(
            tbl=sql.SQL(", ").join(sql.SQL("{s}.{t}.{c}").format(s=schema_id, t=table_id, c=sql.Identifier(c)) for c in non_pk),
            excl=sql.SQL(", ").join(sql.SQL("EXCLUDED.{c}").format(c=sql.Identifier(c)) for c in non_pk),
        )
        on_conflict = sql.SQL("ON CONFLICT ({pk}) DO UPDATE SET {set} WHERE {where}").format(
            pk=sql.SQL(", ").join(pk_ids),
            set=set_clause,
            where=where_clause,
        )
    else:
        on_conflict = sql.SQL("ON CONFLICT ({pk}) DO NOTHING").format(
            pk=sql.SQL(", ").join(pk_ids),
        )

    stmt = sql.SQL("""
        WITH upsert AS (
            INSERT INTO {s}.{t} ({cols})
            SELECT {select_exprs}
            FROM jsonb_array_elements(%s::jsonb) AS t(obj)
            LEFT JOIN {s}.{t} e ON {join_cond}
            {on_conflict}
            RETURNING xmax
        )
        SELECT (xmax = 0) AS inserted, COUNT(1) FROM upsert GROUP BY 1
    """).format(
        s=schema_id,
        t=table_id,
        cols=sql.SQL(", ").join(col_ids),
        select_exprs=select_exprs,
        join_cond=join_cond,
        on_conflict=on_conflict,
    )

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(stmt, [orjson.dumps(rows).decode()])
        counts = {"inserted": 0, "updated": 0, "unchanged": 0}
        for row in cur.fetchall():
            if row["inserted"]:
                counts["inserted"] += row["count"]
            else:
                counts["updated"] += row["count"]
        unchanged = len(rows) - sum(counts.values())
        counts["unchanged"] = unchanged
        return counts
