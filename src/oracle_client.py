"""Oracle EBS database client for write operations.

Supports INSERT and MERGE (upsert) via oracledb thin driver.
"""

import logging

import oracledb
from keboola.component.exceptions import UserException

BATCH_SIZE = 1000


class OracleEBSClient:
    def __init__(self, host: str, port: int, service_name: str, user: str, password: str) -> None:
        self.dsn = f"{host}:{port}/{service_name}"
        self.user = user
        self.password = password

    def _connect(self) -> oracledb.Connection:
        try:
            return oracledb.connect(user=self.user, password=self.password, dsn=self.dsn)
        except oracledb.DatabaseError as e:
            raise UserException(f"Cannot connect to Oracle EBS at {self.dsn}: {e}") from e

    def test_connection(self) -> dict[str, str]:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT banner FROM v$version WHERE ROWNUM = 1")
                row = cur.fetchone()
                return {"version": row[0] if row else "unknown", "dsn": self.dsn}

    def list_operating_units(self) -> list[dict]:
        sql = """
            SELECT organization_id, name
            FROM apps.hr_operating_units
            ORDER BY name
        """
        return self.execute_query(sql)

    def list_ledgers(self) -> list[dict]:
        sql = """
            SELECT ledger_id, name, short_name, currency_code
            FROM apps.gl_ledgers
            WHERE ledger_category_code = 'PRIMARY'
            ORDER BY name
        """
        return self.execute_query(sql)

    def get_table_columns(self, table_name: str) -> list[dict]:
        """Return column metadata for a table (owner.table or just table under APPS)."""
        parts = table_name.upper().split(".")
        owner = parts[0] if len(parts) > 1 else "APPS"
        tname = parts[-1]
        sql = """
            SELECT column_name, data_type, nullable, data_length
            FROM all_tab_columns
            WHERE owner = :owner AND table_name = :table_name
            ORDER BY column_id
        """
        return self.execute_query(sql, params={"owner": owner, "table_name": tname})

    def table_exists(self, table_name: str) -> bool:
        """Check if a table exists."""
        parts = table_name.upper().split(".")
        owner = parts[0] if len(parts) > 1 else "APPS"
        tname = parts[-1]
        sql = """
            SELECT COUNT(*) AS cnt
            FROM all_tables
            WHERE owner = :owner AND table_name = :table_name
        """
        rows = self.execute_query(sql, params={"owner": owner, "table_name": tname})
        return rows[0]["CNT"] > 0 if rows else False

    def execute_query(self, query: str, params: dict | None = None) -> list[dict]:
        """Execute a SELECT query and return rows as dicts."""
        logging.debug("Executing query: %s", query[:200])
        try:
            with self._connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(query, parameters=params or {})
                    columns = [desc[0].upper() for desc in cur.description]
                    rows: list[dict] = []
                    for raw_row in cur:
                        rows.append(dict(zip(columns, raw_row, strict=False)))
                    return rows
        except oracledb.DatabaseError as e:
            raise UserException(f"Oracle query failed: {e}") from e

    def insert_rows(
        self,
        table_name: str,
        columns: list[str],
        rows: list[dict],
        batch_size: int = BATCH_SIZE,
    ) -> int:
        """Insert rows into a table using executemany for performance."""
        if not rows:
            return 0

        col_list = ", ".join(columns)
        placeholders = ", ".join(f":{c}" for c in columns)
        sql = f"INSERT INTO {table_name} ({col_list}) VALUES ({placeholders})"

        total_inserted = 0
        logging.debug("INSERT SQL: %s", sql)

        try:
            with self._connect() as conn:
                with conn.cursor() as cur:
                    for i in range(0, len(rows), batch_size):
                        batch = rows[i : i + batch_size]
                        # Convert rows to list of dicts with only the target columns
                        bind_data = [{c: row.get(c) for c in columns} for row in batch]
                        cur.executemany(sql, bind_data)
                        total_inserted += cur.rowcount
                conn.commit()
            logging.info("Inserted %d rows into %s", total_inserted, table_name)
            return total_inserted
        except oracledb.DatabaseError as e:
            raise UserException(f"Insert into {table_name} failed: {e}") from e

    def upsert_rows(
        self,
        table_name: str,
        columns: list[str],
        rows: list[dict],
        primary_key: list[str],
        batch_size: int = BATCH_SIZE,
    ) -> int:
        """MERGE (upsert) rows into a table — update on PK match, insert otherwise."""
        if not rows:
            return 0

        non_pk_cols = [c for c in columns if c not in primary_key]

        # Build MERGE statement
        on_clause = " AND ".join(f"tgt.{pk} = src.{pk}" for pk in primary_key)

        src_cols = ", ".join(f":{c} AS {c}" for c in columns)
        update_set = ", ".join(f"tgt.{c} = src.{c}" for c in non_pk_cols) if non_pk_cols else None

        tgt_col_list = ", ".join(f"tgt.{c}" for c in columns)
        src_col_list = ", ".join(f"src.{c}" for c in columns)

        sql_parts = [
            f"MERGE INTO {table_name} tgt",
            f"USING (SELECT {src_cols} FROM dual) src",
            f"ON ({on_clause})",
        ]
        if update_set:
            sql_parts.append(f"WHEN MATCHED THEN UPDATE SET {update_set}")
        sql_parts.append(f"WHEN NOT MATCHED THEN INSERT ({tgt_col_list}) VALUES ({src_col_list})")

        sql = "\n".join(sql_parts)
        logging.debug("MERGE SQL: %s", sql)

        total_affected = 0
        try:
            with self._connect() as conn:
                with conn.cursor() as cur:
                    for i in range(0, len(rows), batch_size):
                        batch = rows[i : i + batch_size]
                        bind_data = [{c: row.get(c) for c in columns} for row in batch]
                        cur.executemany(sql, bind_data)
                        total_affected += cur.rowcount
                conn.commit()
            logging.info("Upserted %d rows into %s", total_affected, table_name)
            return total_affected
        except oracledb.DatabaseError as e:
            raise UserException(f"Upsert into {table_name} failed: {e}") from e
