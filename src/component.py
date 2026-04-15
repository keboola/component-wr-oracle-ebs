"""Oracle E-Business Suite Writer.

Writes data from Keboola Storage input tables into Oracle EBS database tables
via direct Oracle DB connection (oracledb thin driver).
Supports preset interface tables or custom target tables with INSERT/UPSERT operations.
"""

import csv
import logging

from keboola.component.base import ComponentBase, sync_action
from keboola.component.exceptions import UserException
from keboola.component.sync_actions import SelectElement

from configuration import Configuration, WriteMode, WriteOperation
from ebs_presets import get_entity_list, get_module_list, get_preset
from oracle_client import OracleEBSClient


class Component(ComponentBase):
    def __init__(self) -> None:
        super().__init__()
        self.params = Configuration(**self.configuration.parameters)

    def run(self) -> None:
        """Main execution — reads input table and writes to Oracle EBS."""
        self.params.validate_row()
        client = self._build_client()

        input_tables = self.get_input_tables_definitions()
        if not input_tables:
            raise UserException("No input tables found. Map at least one input table.")

        input_table = input_tables[0]
        logging.info("Reading input table: %s", input_table.name)

        rows, columns = self._read_input_table(input_table.full_path)
        if not rows:
            logging.warning("Input table is empty — nothing to write.")
            return

        logging.info("Read %d rows with %d columns from input", len(rows), len(columns))

        if self.params.destination.write_mode == WriteMode.PRESET:
            self._write_preset(client, columns, rows)
        else:
            self._write_custom(client, columns, rows)

    # ── Sync Actions ──────────────────────────────────────────────

    @sync_action("testConnection")
    def test_connection(self) -> None:
        self.params.validate_connection()
        result = self._build_client().test_connection()
        logging.info("Connection OK — %s", result["version"])

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
        rows = self._build_client().list_operating_units()
        return [SelectElement(value=str(r["ORGANIZATION_ID"]), label=f"{r['NAME']}") for r in rows]

    @sync_action("listLedgers")
    def list_ledgers(self) -> list[SelectElement]:
        self.params.validate_connection()
        rows = self._build_client().list_ledgers()
        return [SelectElement(value=str(r["LEDGER_ID"]), label=f"{r['NAME']} ({r['CURRENCY_CODE']})") for r in rows]

    @sync_action("listTargetColumns")
    def list_target_columns(self) -> list[SelectElement]:
        """List columns of the target table for column mapping and PK selection."""
        self.params.validate_connection()
        dest = self.configuration.parameters.get("destination", {})

        if dest.get("write_mode") == "preset":
            module = dest.get("module", "")
            entity = dest.get("entity", "")
            preset = get_preset(module, entity)
            if not preset:
                raise UserException("Select a valid module and entity first.")
            table_name = preset["target_table"]
        else:
            table_name = dest.get("target_table", "")
            if not table_name:
                raise UserException("Enter a target table name first.")

        columns = self._build_client().get_table_columns(table_name)
        if not columns:
            raise UserException(f"Table {table_name} not found or has no columns.")
        return [
            SelectElement(value=r["COLUMN_NAME"], label=f"{r['COLUMN_NAME']} ({r['DATA_TYPE']})")
            for r in columns
        ]

    # ── Write Operations ──────────────────────────────────────────

    def _write_preset(self, client: OracleEBSClient, input_columns: list[str], rows: list[dict]) -> None:
        dest = self.params.destination
        preset = get_preset(dest.module, dest.entity)
        if not preset:
            raise UserException(f"Unknown preset: module={dest.module!r}, entity={dest.entity!r}")

        target_table = preset["target_table"]
        default_values = preset.get("default_values", {})
        pk = dest.primary_key or preset.get("default_primary_key", [])

        # Apply column mapping: input_col → target_col
        mapped_rows = self._apply_mapping(rows, dest.column_mapping)

        # Apply default values for columns not present in input
        for row in mapped_rows:
            for col, val in default_values.items():
                if col not in row or row[col] == "" or row[col] is None:
                    row[col] = val

        # Apply org_id if preset is multi-org and org_id is configured
        if preset.get("multi_org") and dest.org_id is not None:
            for row in mapped_rows:
                row.setdefault("ORG_ID", dest.org_id)

        # Apply ledger_id if preset uses ledger
        if preset.get("uses_ledger") and dest.ledger_id is not None:
            for row in mapped_rows:
                row.setdefault("LEDGER_ID", dest.ledger_id)

        # Validate required columns
        all_columns = set()
        for row in mapped_rows:
            all_columns.update(row.keys())

        missing = set(preset["required_columns"]) - all_columns
        if missing:
            raise UserException(
                f"Missing required columns for {preset['name']}: {', '.join(sorted(missing))}. "
                f"Provide them in the input table or via column mapping."
            )

        target_columns = sorted(all_columns)
        logging.info("Writing to %s (%s) — %d rows", target_table, dest.write_operation.value, len(mapped_rows))

        self._execute_write(client, target_table, target_columns, mapped_rows, pk)

    def _write_custom(self, client: OracleEBSClient, input_columns: list[str], rows: list[dict]) -> None:
        dest = self.params.destination
        target_table = dest.target_table
        pk = dest.primary_key

        mapped_rows = self._apply_mapping(rows, dest.column_mapping)

        # Determine target columns from mapped data
        all_columns = set()
        for row in mapped_rows:
            all_columns.update(row.keys())
        target_columns = sorted(all_columns)

        logging.info(
            "Writing to %s (%s, custom) — %d rows", target_table, dest.write_operation.value, len(mapped_rows)
        )

        self._execute_write(client, target_table, target_columns, mapped_rows, pk)

    def _execute_write(
        self,
        client: OracleEBSClient,
        table_name: str,
        columns: list[str],
        rows: list[dict],
        primary_key: list[str],
    ) -> None:
        batch_size = self.params.destination.batch_size

        if self.params.destination.write_operation == WriteOperation.UPSERT:
            affected = client.upsert_rows(table_name, columns, rows, primary_key, batch_size)
        else:
            affected = client.insert_rows(table_name, columns, rows, batch_size)

        logging.info("Write complete — %d rows affected in %s", affected, table_name)

    # ── Helpers ───────────────────────────────────────────────────

    def _build_client(self) -> OracleEBSClient:
        p = self.params
        return OracleEBSClient(
            host=p.db_host,
            port=p.db_port,
            service_name=p.db_service_name,
            user=p.db_user,
            password=p.db_password,
        )

    @staticmethod
    def _read_input_table(path: str) -> tuple[list[dict], list[str]]:
        """Read a CSV input table and return (rows, column_names).

        Column names are uppercased to match Oracle conventions.
        """
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

    @staticmethod
    def _apply_mapping(rows: list[dict], mapping: dict[str, str]) -> list[dict]:
        """Apply column mapping (rename input columns to target columns).

        mapping format: {"INPUT_COL": "TARGET_COL"}
        Columns not in mapping are passed through as-is.
        """
        if not mapping:
            return rows

        # Uppercase the mapping keys for case-insensitive match
        upper_mapping = {k.upper(): v.upper() for k, v in mapping.items()}

        mapped: list[dict] = []
        for row in rows:
            new_row: dict = {}
            for col, val in row.items():
                target_col = upper_mapping.get(col, col)
                new_row[target_col] = val
            mapped.append(new_row)
        return mapped


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
