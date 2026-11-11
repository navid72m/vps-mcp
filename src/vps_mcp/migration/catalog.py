"""The fixed, read-only SQL catalogue (research M2, FR-002).

Every query returns exactly one JSON document. Caller-derived values are never formatted
into SQL: they are passed as psql variables and referenced as :'name' (a quoted literal),
which psql only interpolates for scripts read from a file or stdin — hence queries are sent
on stdin with ``-f -``. Values are additionally validated as identifiers before use.
"""

from __future__ import annotations

import re

from vps_mcp.errors import invalid

IDENT_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")

_USER_TABLES = """
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE c.relkind IN ('r', 'p')
      AND n.nspname NOT IN ('pg_catalog', 'information_schema')
      AND n.nspname NOT LIKE 'pg_toast%' AND n.nspname NOT LIKE 'pg_temp%'
"""

Q_DB_SIZES = """
SELECT coalesce(json_object_agg(datname, pg_database_size(datname)), '{}')
FROM pg_database WHERE NOT datistemplate AND datallowconn;
"""

Q_OVERVIEW = f"""
SELECT json_build_object(
  'size_bytes', pg_database_size(current_database()),
  'tables', (SELECT count(*) {_USER_TABLES}),
  'largest', (SELECT coalesce(json_agg(x), '[]') FROM (
      SELECT n.nspname || '.' || c.relname AS table,
             CASE WHEN c.reltuples < 0 THEN NULL ELSE c.reltuples::bigint END AS est_rows,
             pg_total_relation_size(c.oid) AS bytes
      {_USER_TABLES}
      ORDER BY pg_total_relation_size(c.oid) DESC, 1 LIMIT 20) x),
  'has_odoo', to_regclass('public.ir_module_module') IS NOT NULL
);
"""

Q_TABLES = f"""
WITH t AS (
  SELECT c.oid, n.nspname AS sch, c.relname AS tbl,
         CASE WHEN c.reltuples < 0 THEN NULL ELSE c.reltuples::bigint END AS est,
         pg_total_relation_size(c.oid) AS bytes
  {_USER_TABLES}
)
SELECT coalesce(json_object_agg(t.sch || '.' || t.tbl, json_build_object(
  'est_rows', t.est,
  'total_bytes', t.bytes,
  'columns', (SELECT coalesce(json_object_agg(a.attname, json_build_object(
                  'type', format_type(a.atttypid, a.atttypmod),
                  'nullable', NOT a.attnotnull,
                  'default', pg_get_expr(d.adbin, d.adrelid))), '{{}}')
              FROM pg_attribute a
              LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
              WHERE a.attrelid = t.oid AND a.attnum > 0 AND NOT a.attisdropped),
  'indexes', (SELECT coalesce(json_agg(i.relname ORDER BY i.relname), '[]')
              FROM pg_index x JOIN pg_class i ON i.oid = x.indexrelid
              WHERE x.indrelid = t.oid),
  'constraints', (SELECT coalesce(json_agg(co.conname || ':' || co.contype::text
                                           ORDER BY co.conname), '[]')
                  FROM pg_constraint co WHERE co.conrelid = t.oid)
)), '{{}}') FROM t;
"""

Q_TABLE = """
WITH t AS (
  SELECT c.oid, n.nspname AS sch, c.relname AS tbl,
         CASE WHEN c.reltuples < 0 THEN NULL ELSE c.reltuples::bigint END AS est,
         pg_total_relation_size(c.oid) AS bytes
  FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
  WHERE c.relkind IN ('r', 'p') AND n.nspname = :'sch' AND c.relname = :'tbl'
)
SELECT coalesce((SELECT json_build_object(
  'table', t.sch || '.' || t.tbl,
  'est_rows', t.est,
  'bytes', t.bytes,
  'columns', (SELECT coalesce(json_agg(json_build_object(
                  'name', a.attname,
                  'type', format_type(a.atttypid, a.atttypmod),
                  'nullable', NOT a.attnotnull,
                  'default', pg_get_expr(d.adbin, d.adrelid)) ORDER BY a.attnum), '[]')
              FROM pg_attribute a
              LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
              WHERE a.attrelid = t.oid AND a.attnum > 0 AND NOT a.attisdropped),
  'indexes', (SELECT coalesce(json_agg(pg_get_indexdef(x.indexrelid) ORDER BY 1), '[]')
              FROM pg_index x WHERE x.indrelid = t.oid),
  'constraints', (SELECT coalesce(json_agg(co.conname || ': ' || pg_get_constraintdef(co.oid)
                                           ORDER BY co.conname), '[]')
                  FROM pg_constraint co WHERE co.conrelid = t.oid)
) FROM t), 'null'::json);
"""

# Only valid when has_odoo is true (the planner rejects missing relations at parse time).
Q_MODULES = """
SELECT coalesce(json_object_agg(name, json_build_object(
  'state', state, 'version', latest_version)), '{}')
FROM ir_module_module;
"""

Q_VIEWS = """
SELECT coalesce(json_object_agg(k, v), '{}') FROM (
  SELECT DISTINCT ON (v.id)
         coalesce(md.module || '.' || md.name, 'id:' || v.id) AS k,
         json_build_object(
           'active', v.active,
           'inherit', CASE WHEN v.inherit_id IS NULL THEN NULL
                           ELSE coalesce(pmd.module || '.' || pmd.name, 'id:' || v.inherit_id) END,
           'module', md.module) AS v
  FROM ir_ui_view v
  LEFT JOIN ir_model_data md ON md.model = 'ir.ui.view' AND md.res_id = v.id
  LEFT JOIN ir_model_data pmd ON pmd.model = 'ir.ui.view' AND pmd.res_id = v.inherit_id
  ORDER BY v.id, md.id
) s;
"""

Q_VIEW_STATS = """
SELECT json_build_object(
  'inactive', (SELECT count(*) FROM ir_ui_view WHERE NOT active),
  'orphaned', (SELECT count(DISTINCT v.id)
               FROM ir_ui_view v
               JOIN ir_model_data md ON md.model = 'ir.ui.view' AND md.res_id = v.id
               LEFT JOIN ir_module_module m ON m.name = md.module
               LEFT JOIN ir_model_data pmd ON pmd.model = 'ir.ui.view' AND pmd.res_id = v.inherit_id
               LEFT JOIN ir_module_module pm ON pm.name = pmd.module
               WHERE v.inherit_id IS NOT NULL AND v.active
                 AND (m.state IS DISTINCT FROM 'installed'
                      OR (pmd.module IS NOT NULL AND pm.state IS DISTINCT FROM 'installed')))
);
"""

# :'tables' is a JSON array of validated "schema.table" names.
Q_EXACT_COUNTS = """
SELECT coalesce(json_object_agg(t, (xpath('/row/c/text()', query_to_xml(
  format('SELECT count(*) AS c FROM %I.%I', split_part(t, '.', 1), split_part(t, '.', 2)),
  false, true, '')))[1]::text::bigint), '{}')
FROM json_array_elements_text(:'tables'::json) AS t;
"""

Q_WRITE_COUNTERS = """
SELECT coalesce(json_object_agg(d.datname, json_build_object(
  'writes', d.tup_inserted + d.tup_updated + d.tup_deleted,
  'sessions', (SELECT count(*) FROM pg_stat_activity a
               WHERE a.datname = d.datname AND a.backend_type = 'client backend'
                 AND a.pid <> pg_backend_pid()))), '{}')
FROM pg_stat_database d WHERE d.datname IS NOT NULL;
"""

Q_DB_INFO = """
SELECT json_build_object(
  'exists', count(*) > 0,
  'owner', max(pg_get_userbyid(datdba)),
  'comment', max(shobj_description(oid, 'pg_database')),
  'size_bytes', max(pg_database_size(oid)),
  'sessions', (SELECT count(*) FROM pg_stat_activity a
               WHERE a.datname = :'db' AND a.backend_type = 'client backend'
                 AND a.pid <> pg_backend_pid()))
FROM pg_database WHERE datname = :'db';
"""

Q_STALE_MODULES = """
SELECT coalesce(json_agg(name ORDER BY name), '[]') FROM ir_module_module
WHERE state IN ('installed', 'to upgrade') AND split_part(latest_version, '.', 1) = :'major';
"""

CATALOG: dict[str, str] = {
    "db_sizes": Q_DB_SIZES,
    "overview": Q_OVERVIEW,
    "tables": Q_TABLES,
    "table": Q_TABLE,
    "modules": Q_MODULES,
    "views": Q_VIEWS,
    "view_stats": Q_VIEW_STATS,
    "exact_counts": Q_EXACT_COUNTS,
    "write_counters": Q_WRITE_COUNTERS,
    "db_info": Q_DB_INFO,
    "stale_modules": Q_STALE_MODULES,
}
_BY_SQL = {v: k for k, v in CATALOG.items()}


def name_of(sql: str) -> str | None:
    """Reverse lookup used by the executor guard and by the test fake."""
    return _BY_SQL.get(sql)


def check_identifier(value: str, field: str = "identifier") -> str:
    if not isinstance(value, str) or not IDENT_RE.fullmatch(value):
        raise invalid(f"{field}: {value!r} must match ^[a-z_][a-z0-9_]{{0,62}}$")
    return value


def split_table(value: str) -> tuple[str, str]:
    """'res_partner' -> ('public', 'res_partner'); 'sch.tbl' -> ('sch', 'tbl')."""
    if not isinstance(value, str):
        raise invalid("table: must be a string")
    parts = value.split(".")
    if len(parts) == 1:
        return "public", check_identifier(parts[0], "table")
    if len(parts) == 2:
        return check_identifier(parts[0], "schema"), check_identifier(parts[1], "table")
    raise invalid("table: use 'table' or 'schema.table'")
