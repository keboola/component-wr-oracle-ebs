"""Oracle E-Business Suite Writer.

Writes data from Keboola Storage into Oracle EBS via two methods:

1. **Interface Tables (JDBC)**: Bulk INSERT → submit concurrent program → check status
   Used for: AP invoices, GL journals, PO, INV, OM

2. **ISG REST API**: Synchronous per-row API calls
   Used for: TCA parties/accounts, AR invoices
"""

import csv
import hashlib
import json as _json
import logging

from keboola.component.base import ComponentBase, sync_action
from keboola.component.exceptions import UserException
from keboola.component.sync_actions import SelectElement
from sshtunnel import BaseSSHTunnelForwarderError, SSHTunnelForwarder

from configuration import ColumnMapping, Configuration, WriteMode, WriteOperation
from ebs_presets import get_entity_list, get_module_list, get_preset
from ebs_rest_client import EBSRestClient
from oracle_client import OracleEBSClient
from ssh_utils import SSHException, get_private_key


class Component(ComponentBase):
    def __init__(self) -> None:
        super().__init__()
        self.params = Configuration(**self.configuration.parameters)
        self._ssh_tunnel: SSHTunnelForwarder | None = None
        self.rest_client: EBSRestClient | None = (
            EBSRestClient(
                base_url=self.params.rest_url,
                user=self.params.rest_user,
                password=self.params.rest_password,
                default_org_id=self.params.default_org_id,
            )
            if self.params.rest_url
            else None
        )

    def __del__(self) -> None:
        tunnel = getattr(self, "_ssh_tunnel", None)
        if tunnel is not None and getattr(tunnel, "is_active", False):
            try:
                tunnel.stop()
            except Exception:
                pass

    # ── SSH tunnel + DB client factory ────────────────────────────

    def _ensure_ssh_tunnel(self) -> None:
        """Lazily start the SSH tunnel if `ssh_options.enabled` is true. Idempotent."""
        ssh = self.params.ssh_options
        if not ssh.enabled:
            return
        if self._ssh_tunnel is not None and self._ssh_tunnel.is_active:
            return
        try:
            pkey = get_private_key(ssh.keys.private, None)
        except SSHException as e:
            raise UserException(f"SSH private key is invalid: {e}") from e

        self._ssh_tunnel = SSHTunnelForwarder(
            ssh_address_or_host=(ssh.ssh_host, ssh.ssh_port),
            ssh_username=ssh.user,
            ssh_pkey=pkey,
            remote_bind_address=(self.params.db_host, self.params.db_port),
            local_bind_address=("127.0.0.1", self.params.db_port),
            ssh_config_file=None,
            allow_agent=False,
        )
        try:
            self._ssh_tunnel.start()
        except BaseSSHTunnelForwarderError as e:
            raise UserException("Failed to establish SSH connection. Recheck SSH configuration.") from e
        logging.info(
            "SSH tunnel established: %s:%d via %s@%s:%d",
            self.params.db_host,
            self.params.db_port,
            ssh.user,
            ssh.ssh_host,
            ssh.ssh_port,
        )

    def _build_db_client(self) -> OracleEBSClient:
        """Construct an OracleEBSClient, routing through the SSH tunnel if enabled."""
        self._ensure_ssh_tunnel()
        host = self.params.db_host
        port = self.params.db_port
        if self._ssh_tunnel is not None and self._ssh_tunnel.is_active:
            host = "127.0.0.1"
            port = self._ssh_tunnel.local_bind_port
        return OracleEBSClient(
            host=host,
            port=port,
            service_name=self.params.db_service_name,
            user=self.params.db_user,
            password=self.params.db_password,
            apps_user=self.params.apps_user,
            apps_password=self.params.apps_password,
        )

    @staticmethod
    def _raise_with_rejection_details(
        client: OracleEBSClient, status_check: dict | None, group_id: str | int, original: UserException
    ) -> None:
        """Augment a concurrent-program failure with row-level rejection reasons when possible.

        Many EBS interface programs (APXIIMPT, GLLEZL, POXPOPDOI, …) signal "rows rejected"
        via status_code='E' + a generic completion_text. The actual reasons live in
        per-module rejection tables (e.g. AP_INTERFACE_REJECTIONS) — surface them via the
        preset's `rejection_query` so the user sees what's actually wrong with the data.
        """
        if not status_check or "rejection_query" not in status_check:
            raise original
        details = client.fetch_rejection_details(status_check["rejection_query"], group_id)
        if not details:
            raise original
        lines = [f"  - {row}" for row in details]
        message = f"{original}\n\nRejected rows ({len(details)}):\n" + "\n".join(lines)
        raise UserException(message) from original

    def run(self) -> None:
        """Main execution — reads input table and writes to Oracle EBS."""
        try:
            self.params.validate_row()

            input_tables = self.get_input_tables_definitions()
            if not input_tables:
                raise UserException("No input tables found. Map at least one input table.")

            input_table = input_tables[0]
            logging.info("Reading input table: %s", input_table.name)

            rows, columns = _read_input_table(input_table.full_path)
            if not rows:
                logging.warning("Input table is empty — nothing to write.")
                return

            logging.info("Read %d rows with %d columns from input", len(rows), len(columns))

            if self.params.destination.write_mode == WriteMode.PRESET:
                self._write_preset(rows)
            else:
                self._write_custom(rows)
        finally:
            if self._ssh_tunnel is not None and self._ssh_tunnel.is_active:
                self._ssh_tunnel.stop()

    # ── Sync Actions ──────────────────────────────────────────────

    @sync_action("testConnection")
    def test_connection(self) -> None:
        self.params.validate_connection()
        result = self._build_db_client().test_connection()
        logging.info("DB Connection OK — %s", result["version"])

    @sync_action("testRestConnection")
    def test_rest_connection(self) -> None:
        self.params.validate_rest_connection()
        if self.rest_client is None:
            raise UserException("REST API is not enabled in connection settings.")
        rest_result = self.rest_client.test_connection()
        logging.info("REST Connection OK — %s", rest_result["url"])

    @sync_action("testAppsConnection")
    def test_apps_connection(self) -> None:
        self.params.validate_apps_connection()
        result = self._build_db_client().test_apps_connection()
        logging.info("APPS Connection OK — user=%s, version=%s", result["user"], result["version"])

    @sync_action("listModules")
    def list_modules(self) -> list[SelectElement]:
        return [SelectElement(value=m["value"], label=m["label"]) for m in get_module_list()]

    @sync_action("listEntities")
    def list_entities(self) -> list[SelectElement]:
        module = self.params.destination.module
        if not module:
            raise UserException("Select a module first, then reload entities.")
        return [SelectElement(value=e["value"], label=e["label"]) for e in get_entity_list(module)]

    @sync_action("listOperatingUnits")
    def list_operating_units(self) -> list[SelectElement]:
        self.params.validate_connection()
        rows = self._build_db_client().list_operating_units()
        return [SelectElement(value=str(r["ORGANIZATION_ID"]), label=f"{r['NAME']}") for r in rows]

    @sync_action("listLedgers")
    def list_ledgers(self) -> list[SelectElement]:
        self.params.validate_connection()
        rows = self._build_db_client().list_ledgers()
        return [SelectElement(value=str(r["LEDGER_ID"]), label=f"{r['NAME']} ({r['CURRENCY_CODE']})") for r in rows]

    @sync_action("listTargetColumns")
    def list_target_columns(self) -> list[SelectElement]:
        """List columns of the target table for column mapping and PK selection.

        For preset mode, returns required + optional columns from the preset metadata.
        EBS interface tables are often editioning views that aren't visible in `all_tables`
        to a writer-only grantee, so the preset's declared columns are the reliable source.
        For custom mode, queries `all_tab_columns` directly.
        """
        dest = self.params.destination

        if dest.write_mode == WriteMode.PRESET:
            preset = get_preset(dest.module, dest.entity)
            if not preset:
                raise UserException("Select a valid module and entity first.")
            all_cols = list(preset.get("required_columns", [])) + list(preset.get("optional_columns", []))
            # Interface-table presets also expose sequence-generated PK columns.
            all_cols.extend(preset.get("sequence_columns", {}).keys())
            return [SelectElement(value=c, label=c) for c in dict.fromkeys(all_cols)]

        # Custom mode — query the actual table via all_tab_columns
        self.params.validate_connection()
        if not dest.target_table:
            raise UserException("Enter a target table name first.")
        columns = self._build_db_client().get_table_columns(dest.target_table)
        if not columns:
            raise UserException(f"Table {dest.target_table} not found or has no columns.")
        return [SelectElement(value=r["COLUMN_NAME"], label=f"{r['COLUMN_NAME']} ({r['DATA_TYPE']})") for r in columns]

    @sync_action("reloadColumns")
    def reload_columns(self) -> dict:
        """Pre-populate the column mapping table with target Oracle columns.

        Existing source assignments are preserved on reload — only new destination
        columns appear with empty source. Returns the full `destination` object so
        the platform's shallow-merge wires it back into the form.
        """
        # Read raw params — sync actions run with partial config and self.params
        # may fail validation if required preset fields aren't filled in yet.
        raw_destination = self.configuration.parameters.get("destination", {})

        target_columns = self._resolve_target_columns(raw_destination)
        if not target_columns:
            raise UserException(
                "No target columns found. Pick a module/entity (preset) or target table (custom) first."
            )

        existing_by_dest = {
            m.get("destination"): m
            for m in raw_destination.get("column_mapping", [])
            if isinstance(m, dict) and m.get("destination")
        }
        new_mapping = [existing_by_dest.get(col, {"source": "", "destination": col}) for col in target_columns]

        return {
            "type": "data",
            "data": {"destination": {**raw_destination, "column_mapping": new_mapping}},
        }

    def _resolve_target_columns(self, raw_destination: dict) -> list[str]:
        write_mode = raw_destination.get("write_mode", "preset")
        if write_mode == "preset":
            module = raw_destination.get("module", "")
            entity = raw_destination.get("entity", "")
            if not module or not entity:
                raise UserException("Select a module and entity first.")
            preset = get_preset(module, entity)
            if not preset:
                raise UserException(f"Unknown preset: module={module!r}, entity={entity!r}")
            cols = list(preset.get("required_columns", [])) + list(preset.get("optional_columns", []))
            cols.extend(preset.get("sequence_columns", {}).keys())
            return list(dict.fromkeys(cols))
        target_table = raw_destination.get("target_table", "")
        if not target_table:
            raise UserException("Enter a target table name first.")
        self.params.validate_connection()
        return [r["COLUMN_NAME"] for r in self._build_db_client().get_table_columns(target_table)]

    # ── Write Paths ───────────────────────────────────────────────

    def _write_preset(self, rows: list[dict]) -> None:
        dest = self.params.destination
        preset = get_preset(dest.module, dest.entity)
        if not preset:
            raise UserException(f"Unknown preset: module={dest.module!r}, entity={dest.entity!r}")

        write_method = preset.get("write_method", "interface_table")

        if write_method == "rest_api":
            self._write_rest_api(preset, rows)
        else:
            self._write_interface_table(preset, rows)

    def _write_custom(self, rows: list[dict]) -> None:
        """Custom mode — direct INSERT/UPSERT into specified Oracle table."""
        dest = self.params.destination
        client = self._build_db_client()

        mapped_rows = _apply_mapping(rows, dest.column_mapping)
        all_columns = _collect_columns(mapped_rows)
        target_columns = sorted(all_columns)

        logging.info("Custom write to %s (%s) — %d rows", dest.target_table, dest.write_operation.value, len(rows))

        if dest.write_operation == WriteOperation.UPSERT:
            client.upsert_rows(dest.target_table, target_columns, mapped_rows, dest.primary_key, dest.batch_size)
        else:
            client.insert_rows(dest.target_table, target_columns, mapped_rows, dest.batch_size)

    # ── Interface Table Path (3-step) ─────────────────────────────

    def _write_interface_table(self, preset: dict, rows: list[dict]) -> None:
        """Step 1: INSERT into interface table.
        Step 2: Submit concurrent program.
        Step 3: Check status.
        """
        dest = self.params.destination
        client = self._build_db_client()
        target_table = preset["target_table"]

        # Apply column mapping
        mapped_rows = _apply_mapping(rows, dest.column_mapping)

        # Apply default values
        for row in mapped_rows:
            for col, val in preset.get("default_values", {}).items():
                if col not in row or row[col] is None or row[col] == "":
                    if val == "SYSDATE":
                        continue  # Let Oracle handle SYSDATE
                    row[col] = val

        # Apply org_id / ledger_id
        if preset.get("multi_org") and dest.org_id is not None:
            for row in mapped_rows:
                row.setdefault("ORG_ID", dest.org_id)

        if preset.get("uses_ledger") and dest.ledger_id is not None:
            for row in mapped_rows:
                row.setdefault("LEDGER_ID", dest.ledger_id)

        # Populate sequence-generated identifier columns (e.g. INVOICE_ID from
        # AP_INVOICES_INTERFACE_S). Batched over a single connection so a large
        # input doesn't issue one TCP/auth handshake per row.
        sequence_cols = preset.get("sequence_columns") or {}
        for col, seq_name in sequence_cols.items():
            missing_idx = [i for i, row in enumerate(mapped_rows) if not row.get(col)]
            if not missing_idx:
                continue
            values = client.next_sequence_values(seq_name, len(missing_idx))
            for idx, value in zip(missing_idx, values, strict=True):
                mapped_rows[idx][col] = value

        # Generate a batch group_id for traceability. Numeric because some EBS
        # interface tables (e.g. PO_HEADERS_INTERFACE.BATCH_ID) use NUMBER columns.
        # Derived deterministically from input content so VCR cassettes replay.
        group_id = _deterministic_group_id(mapped_rows, target_table)
        status_check = preset.get("status_check")
        if status_check:
            group_col = status_check["group_column"]
            for row in mapped_rows:
                row.setdefault(group_col, group_id)

        # Validate required columns
        all_columns = _collect_columns(mapped_rows)
        missing = set(preset["required_columns"]) - all_columns
        if missing:
            raise UserException(
                f"Missing required columns for {preset['name']}: {', '.join(sorted(missing))}. "
                f"Provide them in the input table or via column mapping."
            )

        target_columns = sorted(all_columns)

        # ── Step 1: INSERT into interface table
        logging.info("Step 1/3: Inserting %d rows into %s (group_id=%s)", len(mapped_rows), target_table, group_id)
        client.insert_rows(target_table, target_columns, mapped_rows, dest.batch_size)

        # ── Step 2: Submit concurrent program
        cp = preset.get("concurrent_program")
        if cp and dest.submit_program:
            logging.info("Step 2/3: Submitting concurrent program %s.%s", cp["application"], cp["program"])
            fnd = cp.get("fnd_context", {})
            request_id = client.submit_concurrent_request(
                application=cp["application"],
                program=cp["program"],
                description=cp.get("description", ""),
                arguments=cp.get("arguments", []),
                user_id=fnd.get("user_id", 0),
                resp_id=fnd.get("resp_id", 20639),
                resp_appl_id=fnd.get("resp_appl_id", 200),
            )

            # ── Step 3: Wait for completion and check status
            if dest.wait_for_completion:
                logging.info("Step 3/3: Waiting for request %d to complete...", request_id)
                try:
                    client.wait_for_request(
                        request_id,
                        poll_interval=dest.poll_interval,
                        timeout=dest.poll_timeout,
                    )
                except UserException as wait_err:
                    # Concurrent program reported failure. Some programs (e.g. APXIIMPT)
                    # exit with status='E' even when they ran successfully and merely
                    # rejected rows for business reasons — surface those rejections so
                    # the user sees what's actually wrong with the data.
                    self._raise_with_rejection_details(client, status_check, group_id, wait_err)

                # Check interface table status
                if status_check:
                    statuses = client.check_interface_status(
                        target_table,
                        status_check["group_column"],
                        group_id,
                        status_check["status_column"],
                    )
                    logging.info("Interface table status for group %s: %s", group_id, statuses)
                    rejected = statuses.get("REJECTED", 0)
                    if rejected:
                        self._raise_with_rejection_details(
                            client,
                            status_check,
                            group_id,
                            UserException(f"{rejected} row(s) rejected during interface processing."),
                        )
            else:
                logging.info("Step 3/3: Skipped — wait_for_completion is disabled. Request ID: %d", request_id)
        else:
            logging.info("Step 2-3/3: Skipped — no concurrent program or submit_program disabled.")

    # ── REST API Path ─────────────────────────────────────────────

    def _write_rest_api(self, preset: dict, rows: list[dict]) -> None:
        """Write rows via ISG REST API — one API call per row. Fails fast on first error."""
        self.params.validate_rest_connection()
        dest = self.params.destination
        if self.rest_client is None:
            raise UserException("REST API URL is not configured but the selected preset requires REST.")
        rest_client = self.rest_client
        service = preset["rest_service"]
        org_id = dest.org_id or self.params.default_org_id

        mapped_rows = _apply_mapping(rows, dest.column_mapping)

        # Apply default values
        for row in mapped_rows:
            for col, val in preset.get("default_values", {}).items():
                if col not in row or row[col] is None or row[col] == "":
                    row[col] = val

        # Validate required columns
        all_columns = _collect_columns(mapped_rows)
        missing = set(preset["required_columns"]) - all_columns
        if missing:
            raise UserException(
                f"Missing required columns for {preset['name']}: {', '.join(sorted(missing))}. "
                f"Provide them in the input table or via column mapping."
            )

        logging.info("Writing %d rows via REST API (%s)", len(mapped_rows), service)

        response_id_field = preset.get("response_id_field")
        success_count = 0

        for i, row in enumerate(mapped_rows, 1):
            try:
                result = self._call_rest_service(rest_client, service, row, org_id)
            except UserException:
                logging.error("Row %d failed: %s", i, row)
                raise

            if response_id_field:
                output = result.get("OutputParameters", result)
                logging.info("Row %d: created %s=%s", i, response_id_field, output.get(response_id_field))

            success_count += 1

        logging.info("REST write complete: %d rows succeeded", success_count)

    @staticmethod
    def _call_rest_service(rest_client: EBSRestClient, service: str, row: dict, org_id: int) -> dict:
        """Dispatch to the appropriate REST client method."""
        if service == "create_organization":
            return rest_client.create_organization(row, org_id=org_id)
        if service == "create_person":
            return rest_client.create_person(row, org_id=org_id)
        if service == "create_cust_account":
            account_rec = {k: v for k, v in row.items() if k.startswith("ACCOUNT_") or k == "CUSTOMER_CLASS_CODE"}
            person_rec = {k: v for k, v in row.items() if k.startswith("PERSON_")}
            return rest_client.create_cust_account(account_rec, person_rec if person_rec else None, org_id=org_id)
        if service == "create_ar_invoice":
            header_rec = {
                k: v for k, v in row.items() if k not in ("QUANTITY_INVOICED", "UNIT_SELLING_PRICE", "LINE_DESCRIPTION")
            }
            line = {
                "QUANTITY_INVOICED": row.get("QUANTITY_INVOICED", 1),
                "UNIT_SELLING_PRICE": row.get("UNIT_SELLING_PRICE", 0),
                "DESCRIPTION": row.get("LINE_DESCRIPTION", "Keboola import"),
            }
            return rest_client.create_ar_invoice(header_rec, [line], org_id)
        raise UserException(f"Unknown REST service: {service}")


# ── Standalone utilities ─────────────────────────────────────────


def _deterministic_group_id(rows: list[dict], salt: str = "") -> int:
    """Derive a stable numeric batch id from input rows + target table.

    Used as GROUP_ID / BATCH_ID in EBS interface-table inserts. Deterministic so
    VCR cassettes produce identical SQL on re-record/replay.
    Fits in Oracle NUMBER(15) — truncate to 15 decimal digits.
    """
    payload = _json.dumps([rows, salt], default=str, sort_keys=True)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return int(digest[:12], 16) % 10**15


def _read_input_table(path: str) -> tuple[list[dict], list[str]]:
    """Read a CSV input table. Column names are uppercased to match Oracle conventions."""
    rows: list[dict] = []
    columns: list[str] = []
    with open(path, encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames:
            columns = [c.upper() for c in reader.fieldnames]
        for raw_row in reader:
            row = {k.upper(): (v if v != "" else None) for k, v in raw_row.items()}
            rows.append(row)
    return rows, columns


def _apply_mapping(rows: list[dict], mapping: list[ColumnMapping]) -> list[dict]:
    """Filter to mapped columns and rename to target column names.

    Only columns listed in `mapping` reach the output. Unmapped input columns are dropped.
    Empty mapping is treated as opt-out (passthrough), not "drop everything".
    """
    if not mapping:
        return rows
    rename = {cm.source.upper(): cm.destination.upper() for cm in mapping}
    return [{rename[col.upper()]: val for col, val in row.items() if col.upper() in rename} for row in rows]


def _collect_columns(rows: list[dict]) -> set[str]:
    """Collect all column names present across rows."""
    all_columns: set[str] = set()
    for row in rows:
        all_columns.update(k for k, v in row.items() if v is not None)
    return all_columns


"""Main entrypoint"""
if __name__ == "__main__":
    try:
        comp = Component()
        comp.execute_action()
    except UserException as exc:
        logging.exception(exc)
        exit(1)
    except Exception as exc:
        logging.exception(exc)
        exit(2)
