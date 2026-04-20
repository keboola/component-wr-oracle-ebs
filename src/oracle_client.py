"""Oracle EBS database client for write operations.

Supports INSERT, MERGE (upsert), concurrent program submission via
FND_REQUEST.SUBMIT_REQUEST, and request status polling.

Uses oracledb thin driver.
"""

import logging
import time

import oracledb
from keboola.component.exceptions import UserException

BATCH_SIZE = 1000

# Concurrent request phase/status codes
PHASE_COMPLETE = "C"
STATUS_NORMAL = "C"
STATUS_ERROR = "E"
STATUS_WARNING = "G"


class OracleEBSClient:
    def __init__(self, host: str, port: int, service_name: str, user: str, password: str) -> None:
        self.dsn = f"{host}:{port}/{service_name}"
        self.user = user
        self.password = password

    def _connect(self) -> oracledb.Connection:
        try:
            return oracledb.connect(user=self.user, password=self.password, dsn=self.dsn)
        except oracledb.DatabaseError as e:
            msg = str(e)
            if "DPY-3015" in msg:
                raise UserException(
                    f"Cannot connect to Oracle EBS at {self.dsn}: the database user '{self.user}' "
                    f"has an old password hash (verifier type not supported by the thin driver).\n"
                    f"Fix on the Oracle server side — either:\n"
                    f"  1. Reset the password to regenerate the hash:\n"
                    f"     ALTER USER {self.user} IDENTIFIED BY <same-or-new-password>;\n"
                    f"  2. Allow older verifiers in sqlnet.ora:\n"
                    f"     SQLNET.ALLOWED_LOGON_VERSION_SERVER = 11  (or 12)\n"
                    f"Original error: {e}"
                ) from e
            raise UserException(f"Cannot connect to Oracle EBS at {self.dsn}: {e}") from e

    # ── Connection & Metadata ─────────────────────────────────────

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

    # ── Query ─────────────────────────────────────────────────────

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

    # ── Write Operations ──────────────────────────────────────────

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

    # ── Concurrent Program Submission ─────────────────────────────

    def submit_concurrent_request(
        self,
        application: str,
        program: str,
        description: str = "",
        user_id: int = 0,
        resp_id: int = 20634,
        resp_appl_id: int = 101,
        arguments: list[str] | None = None,
    ) -> int:
        """Submit an EBS concurrent request via FND_REQUEST.SUBMIT_REQUEST.

        Returns the request_id (>0 on success, 0 on failure).

        Args:
            application: EBS application short name (e.g. 'SQLAP', 'SQLGL')
            program: Concurrent program short name (e.g. 'APXIIMPT', 'GLLEZL')
            description: Request description
            user_id: FND user ID (0 = SYSADMIN)
            resp_id: Responsibility ID
            resp_appl_id: Responsibility application ID
            arguments: Up to 100 program arguments (empty strings for unused)
        """
        args = arguments or []
        # Pad to at least 10 arguments (Oracle requires positional args)
        while len(args) < 10:
            args.append("")

        # Build argument bindings
        arg_binds = ", ".join(f":arg{i}" for i in range(len(args)))

        plsql = f"""
        DECLARE
            v_request_id NUMBER;
        BEGIN
            fnd_global.apps_initialize(
                user_id      => :user_id,
                resp_id      => :resp_id,
                resp_appl_id => :resp_appl_id
            );

            v_request_id := fnd_request.submit_request(
                application => :application,
                program     => :program,
                description => :description,
                start_time  => NULL,
                sub_request => FALSE,
                {arg_binds}
            );

            COMMIT;
            :request_id := v_request_id;
        END;
        """

        params = {
            "user_id": user_id,
            "resp_id": resp_id,
            "resp_appl_id": resp_appl_id,
            "application": application,
            "program": program,
            "description": description or f"Keboola {program} import",
        }
        for i, arg in enumerate(args):
            params[f"arg{i}"] = arg

        logging.info("Submitting concurrent request: %s.%s", application, program)

        try:
            with self._connect() as conn:
                with conn.cursor() as cur:
                    request_id_var = cur.var(oracledb.NUMBER)
                    params["request_id"] = request_id_var
                    cur.execute(plsql, parameters=params)
                    request_id = int(request_id_var.getvalue())

            if request_id == 0:
                raise UserException(
                    f"Concurrent request submission failed for {application}.{program}. "
                    f"Check EBS concurrent manager logs."
                )

            logging.info("Concurrent request submitted: request_id=%d", request_id)
            return request_id

        except oracledb.DatabaseError as e:
            raise UserException(f"Failed to submit concurrent request: {e}") from e

    def get_request_status(self, request_id: int) -> dict:
        """Check the status of a concurrent request.

        Returns dict with keys: phase_code, status_code, completion_text.
        Phase: P=Pending, R=Running, C=Complete, I=Inactive
        Status: C=Normal, E=Error, G=Warning, X=Terminated
        """
        sql = """
            SELECT phase_code, status_code, completion_text
            FROM apps.fnd_concurrent_requests
            WHERE request_id = :request_id
        """
        rows = self.execute_query(sql, params={"request_id": request_id})
        if not rows:
            raise UserException(f"Concurrent request {request_id} not found.")
        return rows[0]

    def wait_for_request(self, request_id: int, poll_interval: int = 10, timeout: int = 600) -> dict:
        """Poll until a concurrent request completes or times out.

        Returns the final status dict. Raises UserException on error or timeout.
        """
        start = time.monotonic()
        while True:
            status = self.get_request_status(request_id)
            phase = status.get("PHASE_CODE", "")
            status_code = status.get("STATUS_CODE", "")

            if phase == PHASE_COMPLETE:
                if status_code == STATUS_ERROR:
                    raise UserException(
                        f"Concurrent request {request_id} failed: {status.get('COMPLETION_TEXT', 'unknown error')}"
                    )
                if status_code == STATUS_WARNING:
                    logging.warning(
                        "Concurrent request %d completed with warnings: %s",
                        request_id,
                        status.get("COMPLETION_TEXT", ""),
                    )
                else:
                    logging.info("Concurrent request %d completed successfully.", request_id)
                return status

            elapsed = time.monotonic() - start
            if elapsed > timeout:
                raise UserException(
                    f"Concurrent request {request_id} timed out after {timeout}s. "
                    f"Current phase={phase}, status={status_code}"
                )

            logging.debug(
                "Request %d: phase=%s, status=%s — waiting %ds...",
                request_id,
                phase,
                status_code,
                poll_interval,
            )
            time.sleep(poll_interval)

    # ── Interface Table Status Check ──────────────────────────────

    def check_interface_status(self, table_name: str, group_column: str, group_id: str, status_column: str) -> dict:
        """Check how many rows succeeded/failed in an interface table after processing."""
        sql = f"""
            SELECT {status_column}, COUNT(*) AS cnt
            FROM {table_name}
            WHERE {group_column} = :group_id
            GROUP BY {status_column}
        """
        rows = self.execute_query(sql, params={"group_id": group_id})
        return {r[status_column]: r["CNT"] for r in rows}
