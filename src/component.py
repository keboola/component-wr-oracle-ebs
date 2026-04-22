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

from configuration import Configuration, WriteMode, WriteOperation
from ebs_presets import get_entity_list, get_module_list, get_preset
from ebs_rest_client import EBSRestClient
from oracle_client import OracleEBSClient


class Component(ComponentBase):
    def __init__(self) -> None:
        super().__init__()
        self.params = Configuration(**self.configuration.parameters)

    def run(self) -> None:
        """Main execution — reads input table and writes to Oracle EBS."""
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

    # ── Sync Actions ──────────────────────────────────────────────

    @sync_action("testConnection")
    def test_connection(self) -> None:
        self.params.validate_connection()
        result = self._build_db_client().test_connection()
        logging.info("DB Connection OK — %s", result["version"])

        if self.params.rest_url:
            try:
                self.params.validate_rest_connection()
                rest_result = self._build_rest_client().test_connection()
                logging.info("REST Connection OK — %s", rest_result["url"])
            except Exception as e:
                # REST API is optional for interface-table writes; failure here is a warning, not an error.
                logging.warning("REST API unreachable (optional for interface-table writes): %s", e)

    @sync_action("listModules")
    def list_modules(self) -> list[SelectElement]:
        return [SelectElement(value=m["value"], label=m["label"]) for m in get_module_list()]

    @sync_action("listEntities")
    def list_entities(self) -> list[SelectElement]:
        module = self.configuration.parameters.get("destination", {}).get("module", "")
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
        dest = self.configuration.parameters.get("destination", {})

        if dest.get("write_mode") == "preset":
            module = dest.get("module", "")
            entity = dest.get("entity", "")
            preset = get_preset(module, entity)
            if not preset:
                raise UserException("Select a valid module and entity first.")
            all_cols = list(preset.get("required_columns", [])) + list(preset.get("optional_columns", []))
            # Interface-table presets also expose sequence-generated PK columns.
            all_cols.extend(preset.get("sequence_columns", {}).keys())
            return [SelectElement(value=c, label=c) for c in dict.fromkeys(all_cols)]

        # Custom mode — query the actual table via all_tab_columns
        self.params.validate_connection()
        table_name = dest.get("target_table", "")
        if not table_name:
            raise UserException("Enter a target table name first.")
        columns = self._build_db_client().get_table_columns(table_name)
        if not columns:
            raise UserException(f"Table {table_name} not found or has no columns.")
        return [SelectElement(value=r["COLUMN_NAME"], label=f"{r['COLUMN_NAME']} ({r['DATA_TYPE']})") for r in columns]

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
        # AP_INVOICES_INTERFACE_S). Uses SELECT seq.NEXTVAL once per row.
        sequence_cols = preset.get("sequence_columns") or {}
        if sequence_cols:
            client_for_seq = client
            for row in mapped_rows:
                for col, seq_name in sequence_cols.items():
                    if row.get(col):
                        continue
                    seq_rows = client_for_seq.execute_query(f"SELECT {seq_name}.NEXTVAL AS NEXTVAL FROM dual")
                    row[col] = seq_rows[0]["NEXTVAL"] if seq_rows else None

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
            request_id = client.submit_concurrent_request(
                application=cp["application"],
                program=cp["program"],
                description=cp.get("description", ""),
                arguments=cp.get("arguments", []),
            )

            # ── Step 3: Wait for completion and check status
            if dest.wait_for_completion:
                logging.info("Step 3/3: Waiting for request %d to complete...", request_id)
                client.wait_for_request(
                    request_id,
                    poll_interval=dest.poll_interval,
                    timeout=dest.poll_timeout,
                )

                # Check interface table status
                if status_check:
                    statuses = client.check_interface_status(
                        target_table,
                        status_check["group_column"],
                        group_id,
                        status_check["status_column"],
                    )
                    logging.info("Interface table status for group %s: %s", group_id, statuses)
            else:
                logging.info("Step 3/3: Skipped — wait_for_completion is disabled. Request ID: %d", request_id)
        else:
            logging.info("Step 2-3/3: Skipped — no concurrent program or submit_program disabled.")

    # ── REST API Path ─────────────────────────────────────────────

    def _write_rest_api(self, preset: dict, rows: list[dict]) -> None:
        """Write rows via ISG REST API — one API call per row."""
        self.params.validate_rest_connection()
        dest = self.params.destination
        rest_client = self._build_rest_client()
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

        success_count = 0
        error_count = 0
        response_id_field = preset.get("response_id_field")

        for i, row in enumerate(mapped_rows, 1):
            try:
                result = self._call_rest_service(rest_client, service, row, org_id)

                if response_id_field:
                    output = result.get("OutputParameters", result)
                    created_id = output.get(response_id_field)
                    logging.info("Row %d: created %s=%s", i, response_id_field, created_id)

                success_count += 1
            except UserException:
                error_count += 1
                logging.error("Row %d failed: %s", i, row)
                raise

        logging.info("REST write complete: %d succeeded, %d failed", success_count, error_count)

    @staticmethod
    def _call_rest_service(rest_client: EBSRestClient, service: str, row: dict, org_id: int) -> dict:
        """Dispatch to the appropriate REST client method."""
        if service == "create_organization":
            return rest_client.create_organization(row)
        if service == "create_person":
            return rest_client.create_person(row)
        if service == "create_cust_account":
            account_rec = {k: v for k, v in row.items() if k.startswith("ACCOUNT_") or k == "CUSTOMER_CLASS_CODE"}
            person_rec = {k: v for k, v in row.items() if k.startswith("PERSON_")}
            return rest_client.create_cust_account(account_rec, person_rec if person_rec else None)
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

    # ── Client Builders ───────────────────────────────────────────

    def _build_db_client(self) -> OracleEBSClient:
        p = self.params
        return OracleEBSClient(
            host=p.db_host,
            port=p.db_port,
            service_name=p.db_service_name,
            user=p.db_user,
            password=p.db_password,
        )

    def _build_rest_client(self) -> EBSRestClient:
        p = self.params
        return EBSRestClient(
            base_url=p.rest_url,
            user=p.rest_user,
            password=p.rest_password,
        )


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


def _apply_mapping(rows: list[dict], mapping: dict[str, str]) -> list[dict]:
    """Apply column mapping — rename input columns to target columns."""
    if not mapping:
        return rows
    upper_mapping = {k.upper(): v.upper() for k, v in mapping.items()}
    mapped: list[dict] = []
    for row in rows:
        new_row: dict = {}
        for col, val in row.items():
            target_col = upper_mapping.get(col, col)
            new_row[target_col] = val
        mapped.append(new_row)
    return mapped


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
