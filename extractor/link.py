from __future__ import annotations

from sqlalchemy import Engine, inspect

from .dump import q


def link(dest: Engine) -> dict[str, list[str]]:
    """Build `_image_links` and add `<col>_local` to single-image columns.

    Original values are never modified. Returns {table: [columns that got *_local]}.
    """
    added: dict[str, list[str]] = {}
    with dest.begin() as c:
        c.exec_driver_sql("DROP TABLE IF EXISTS _image_links")
        c.exec_driver_sql(
            """
            CREATE TABLE _image_links AS
            SELECT r.table_name, r.column_name, r.src_rowid, r.row_pk, r.raw_ref, r.position,
                   CASE WHEN i.status = 'ok' THEN i.local_path END AS local_path,
                   i.status, i.url
            FROM _image_refs r LEFT JOIN _images i ON i.job_key = r.job_key
            """
        )
        single = c.exec_driver_sql(
            """
            SELECT table_name, column_name FROM (
                SELECT table_name, column_name, MAX(n) AS m FROM (
                    SELECT table_name, column_name, src_rowid, COUNT(*) AS n
                    FROM _image_refs GROUP BY 1, 2, 3
                ) GROUP BY 1, 2
            ) WHERE m = 1
            """
        ).fetchall()

    insp = inspect(dest)
    for table, column in single:
        existing = {col["name"] for col in insp.get_columns(table)}
        local = f"{column}_local"
        with dest.begin() as c:
            if local not in existing:
                c.exec_driver_sql(f"ALTER TABLE {q(table)} ADD COLUMN {q(local)} TEXT")
            c.exec_driver_sql(
                f"UPDATE {q(table)} SET {q(local)} = (SELECT l.local_path FROM _image_links l "
                f"WHERE l.table_name = ? AND l.column_name = ? AND l.src_rowid = {q(table)}.rowid LIMIT 1)",
                (table, column),
            )
        added.setdefault(table, []).append(local)
    return added
