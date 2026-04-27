"""Oracle EBS database client for write operations.

Supports INSERT, MERGE (upsert), concurrent program submission via
FND_REQUEST.SUBMIT_REQUEST, and request status polling.

Uses oracledb in thick mode when Oracle Instant Client is available
(required for Oracle EBS servers with `sec_case_sensitive_logon=FALSE` or
legacy password verifiers), otherwise falls back to thin mode.
"""

import logging
import os
import time
from typing import Any

import oracledb
from keboola.component.exceptions import UserException

BATCH_SIZE = 1000

# Concurrent request phase/status codes
PHASE_COMPLETE = "C"
STATUS_NORMAL = "C"
STATUS_ERROR = "E"
STATUS_WARNING = "G"

# Oracle Instant Client search order. Docker image installs to /opt/oracle/instantclient;
# local dev commonly uses ~/oracle/instantclient_<ver>. Override with ORACLE_CLIENT_LIB_DIR.
_INSTANT_CLIENT_PATHS = [
    os.environ.get("ORACLE_CLIENT_LIB_DIR", ""),
    "/opt/oracle/instantclient",
    os.path.expanduser("~/oracle/instantclient_23_3"),
    os.path.expanduser("~/oracle/instantclient"),
]


def _init_thick_mode_if_available() -> bool:
    """Enable oracledb thick mode if Instant Client is installed. Returns True on success.

    Thick mode is required for Oracle servers using `sec_case_sensitive_logon=FALSE` or
    advertising only the 10G password verifier — both common on older Oracle EBS installs.

    Set env var `ORACLE_THIN_MODE_ONLY=1` to skip thick-mode init (used by VCR recording
    so the cursor wrapper in keboola-vcr's DB adapter — which is thin-only — keeps working).
    """
    if os.environ.get("ORACLE_THIN_MODE_ONLY") == "1":
        logging.info("ORACLE_THIN_MODE_ONLY=1 set; using thin mode")
        return False

    tried: list[str] = []
    for candidate in _INSTANT_CLIENT_PATHS:
        if not candidate:
            continue
        if not os.path.isdir(candidate):
            continue
        tried.append(candidate)
        try:
            oracledb.init_oracle_client(lib_dir=candidate)
            logging.info("Oracle thick mode enabled: lib_dir=%s", candidate)
            return True
        except oracledb.ProgrammingError:
            # Already initialized — idempotent, still thick mode
            return True
        except Exception as e:
            # Surface the real failure reason so the operator can fix it.
            logging.warning("Failed to init Oracle thick mode with %s: %s", candidate, e)
    if tried:
        logging.warning(
            "Oracle Instant Client directories were present but none loaded: %s. Using thin mode.",
            ", ".join(tried),
        )
    else:
        logging.info("Oracle Instant Client not found; using thin mode")
    return False


# Initialize thick mode at import time — once per process.
_init_thick_mode_if_available()


def _safe_commit(conn: Any) -> None:
    """Commit if the connection supports it; no-op otherwise.

    keboola-vcr's `_RecordingConnection` wrapper doesn't proxy `commit`; during
    cassette recording/replay we rely on the adapter to capture the SQL anyway,
    so skipping commit is safe in tests.
    """
    commit = getattr(conn, "commit", None)
    if callable(commit):
        commit()


class OracleEBSClient:
    def __init__(self, host: str, port: int, service_name: str, user: str, password: str) -> None:
        self.dsn = f"{host}:{port}/{service_name}"
        self.user = user
        self.password = password

    def _connect(self) -> oracledb.Connection:
        try:
            conn = oracledb.connect(user=self.user, password=self.password, dsn=self.dsn)
            # Permit ISO-8601 style date strings from CSV input to be implicit-cast into DATE columns.
            with conn.cursor() as cur:
                cur.execute("ALTER SESSION SET NLS_DATE_FORMAT = 'YYYY-MM-DD'")
            return conn
        except oracledb.DatabaseError as e:
            msg = str(e)
            if "DPY-3015" in msg:
                thin_active = oracledb.is_thin_mode()
                reason = (
                    "the bundled Oracle Instant Client failed to load at import "
                    "(check startup logs for `Failed to init Oracle thick mode`)"
                    if thin_active
                    else "Oracle server is advertising the legacy 10G password verifier"
                )
                raise UserException(
                    f"Cannot connect to Oracle EBS at {self.dsn}: {reason}. "
                    f"The user '{self.user}' likely has only the 10G verifier, which the thin "
                    f"driver doesn't support. Either:\n"
                    f"  1. Let the component use thick mode (default in the Keboola-provided "
                    f"Docker image). If you're running locally, install Oracle Instant Client "
                    f"and set ORACLE_CLIENT_LIB_DIR.\n"
                    f"  2. Ask your DBA to reset the user's password so 11G/12C verifiers are "
                    f"generated:  ALTER USER {self.user} IDENTIFIED BY <same-or-new-password>;\n"
                    f"  3. Ensure `sec_case_sensitive_logon` is TRUE at the DB/PDB level.\n"
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
                    # fetchall() works with both real oracledb cursors and the
                    # keboola-vcr recording cursor shim (which doesn't implement __iter__).
                    raw_rows = cur.fetchall() or []
                    return [dict(zip(columns, row, strict=False)) for row in raw_rows]
        except oracledb.DatabaseError as e:
            raise UserException(f"Oracle query failed: {e}") from e

    def next_sequence_values(self, sequence_name: str, count: int) -> list[int]:
        """Fetch `count` NEXTVAL values from an Oracle sequence over a single connection.

        Uses one cursor per SELECT so the keboola-vcr recording shim (which flushes
        one cassette entry per cursor close) captures each call as a separate entry —
        keeps existing cassettes that pre-date this method replay-compatible.
        """
        if count <= 0:
            return []
        sql = f"SELECT {sequence_name}.NEXTVAL AS NEXTVAL FROM dual"
        values: list[int] = []
        try:
            with self._connect() as conn:
                for _ in range(count):
                    with conn.cursor() as cur:
                        # Pass parameters={} explicitly so keboola-vcr hashes match the
                        # cassettes recorded via execute_query (which also defaults to {}).
                        cur.execute(sql, parameters={})
                        raw_rows = cur.fetchall() or []
                        values.append(int(raw_rows[0][0]) if raw_rows else 0)
            return values
        except oracledb.DatabaseError as e:
            raise UserException(f"Sequence {sequence_name}.NEXTVAL failed: {e}") from e

    @staticmethod
    def _execute_batch(conn: Any, sql: str, bind_data: list[dict]) -> int:
        """Execute a parametrized statement once per row, using a fresh cursor each time.

        Fresh cursor per row is deliberate: keboola-vcr's `_RecordingCursor` only flushes
        one cassette entry per cursor lifetime (on `close()`). Reusing a single cursor
        for multiple executes overwrites the in-progress entry, so only the last call
        would be recorded. Creating a new cursor per row gives one cassette entry per
        statement and keeps recording/replay symmetric.
        """
        for row in bind_data:
            with conn.cursor() as cur:
                cur.execute(sql, row)
        return len(bind_data)

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
                for i in range(0, len(rows), batch_size):
                    batch = rows[i : i + batch_size]
                    bind_data = [{c: row.get(c) for c in columns} for row in batch]
                    total_inserted += self._execute_batch(conn, sql, bind_data)
                _safe_commit(conn)
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
                for i in range(0, len(rows), batch_size):
                    batch = rows[i : i + batch_size]
                    bind_data = [{c: row.get(c) for c in columns} for row in batch]
                    total_affected += self._execute_batch(conn, sql, bind_data)
                _safe_commit(conn)
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
            apps.fnd_global.apps_initialize(
                user_id      => :user_id,
                resp_id      => :resp_id,
                resp_appl_id => :resp_appl_id
            );

            v_request_id := apps.fnd_request.submit_request(
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

    def check_interface_status(
        self, table_name: str, group_column: str, group_id: str | int, status_column: str
    ) -> dict:
        """Check how many rows succeeded/failed in an interface table after processing."""
        sql = f"""
            SELECT {status_column}, COUNT(*) AS cnt
            FROM {table_name}
            WHERE {group_column} = :group_id
            GROUP BY {status_column}
        """
        rows = self.execute_query(sql, params={"group_id": group_id})
        return {r[status_column]: r["CNT"] for r in rows}
