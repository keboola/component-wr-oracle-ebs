"""Unit tests for EBS writer presets, configuration, and mapping (no DB connection needed)."""

import csv
import tempfile

import pytest
from keboola.component.exceptions import UserException

from configuration import Configuration, WriteMode, WriteOperation
from ebs_presets import get_entity_list, get_module_list, get_preset


class TestModuleList:
    def test_returns_all_modules(self) -> None:
        modules = get_module_list()
        keys = {m["value"] for m in modules}
        assert {"ap", "ar", "gl", "po", "inv", "om"} == keys

    def test_each_module_has_label(self) -> None:
        for m in get_module_list():
            assert m["label"]
            assert m["value"]


class TestEntityList:
    def test_ap_entities(self) -> None:
        entities = get_entity_list("ap")
        keys = {e["value"] for e in entities}
        assert "ap_invoices_interface" in keys
        assert "ap_invoice_lines_interface" in keys

    def test_gl_entities(self) -> None:
        entities = get_entity_list("gl")
        keys = {e["value"] for e in entities}
        assert "gl_interface" in keys

    def test_po_entities(self) -> None:
        entities = get_entity_list("po")
        keys = {e["value"] for e in entities}
        assert "po_headers_interface" in keys
        assert "po_lines_interface" in keys

    def test_unknown_module_returns_empty(self) -> None:
        assert get_entity_list("nonexistent") == []


class TestGetPreset:
    def test_known_preset(self) -> None:
        preset = get_preset("ap", "ap_invoices_interface")
        assert preset is not None
        assert preset["name"] == "AP Invoices Interface"
        assert preset["target_table"] == "APPS.AP_INVOICES_INTERFACE"

    def test_gl_interface_preset(self) -> None:
        preset = get_preset("gl", "gl_interface")
        assert preset is not None
        assert preset["uses_ledger"] is True
        assert "STATUS" in preset["required_columns"]
        assert preset["default_values"]["STATUS"] == "NEW"

    def test_unknown_preset_returns_none(self) -> None:
        assert get_preset("ap", "nonexistent") is None

    def test_preset_has_required_columns(self) -> None:
        preset = get_preset("ap", "ap_invoices_interface")
        assert "INVOICE_ID" in preset["required_columns"]
        assert "INVOICE_NUM" in preset["required_columns"]
        assert "SOURCE" in preset["required_columns"]

    def test_preset_has_default_values(self) -> None:
        preset = get_preset("ap", "ap_invoices_interface")
        assert preset["default_values"]["SOURCE"] == "KEBOOLA"

    def test_inv_preset_defaults(self) -> None:
        preset = get_preset("inv", "mtl_transactions_interface")
        assert preset["default_values"]["SOURCE_CODE"] == "KEBOOLA"
        assert preset["default_values"]["PROCESS_FLAG"] == "1"


class TestConfiguration:
    def test_flatten_connection(self) -> None:
        cfg = Configuration(
            **{
                "connection": {
                    "db_host": "localhost",
                    "db_port": 1521,
                    "db_service_name": "EBSDB",
                    "#db_user": "user",
                    "#db_password": "pass",
                },
                "destination": {"write_mode": "preset", "module": "ap", "entity": "ap_invoices_interface"},
            }
        )
        assert cfg.db_host == "localhost"
        assert cfg.db_user == "user"
        assert cfg.db_password == "pass"

    def test_defaults(self) -> None:
        cfg = Configuration()
        assert cfg.db_host == ""
        assert cfg.db_port == 1521
        assert cfg.destination.write_mode == WriteMode.PRESET
        assert cfg.destination.write_operation == WriteOperation.INSERT
        assert cfg.destination.batch_size == 1000

    def test_validate_row_preset_missing_module(self) -> None:
        cfg = Configuration(
            **{
                "connection": {
                    "db_host": "h",
                    "db_service_name": "s",
                    "#db_user": "u",
                    "#db_password": "p",
                },
                "destination": {"write_mode": "preset", "module": "", "entity": ""},
            }
        )
        with pytest.raises(UserException, match="module"):
            cfg.validate_row()

    def test_validate_row_custom_missing_table(self) -> None:
        cfg = Configuration(
            **{
                "connection": {
                    "db_host": "h",
                    "db_service_name": "s",
                    "#db_user": "u",
                    "#db_password": "p",
                },
                "destination": {"write_mode": "custom", "target_table": ""},
            }
        )
        with pytest.raises(UserException, match="target_table"):
            cfg.validate_row()

    def test_validate_row_upsert_missing_pk(self) -> None:
        cfg = Configuration(
            **{
                "connection": {
                    "db_host": "h",
                    "db_service_name": "s",
                    "#db_user": "u",
                    "#db_password": "p",
                },
                "destination": {
                    "write_mode": "custom",
                    "target_table": "APPS.SOME_TABLE",
                    "write_operation": "upsert",
                    "primary_key": [],
                },
            }
        )
        with pytest.raises(UserException, match="primary_key"):
            cfg.validate_row()

    def test_validate_connection_missing_host(self) -> None:
        cfg = Configuration()
        with pytest.raises(UserException, match="db_host"):
            cfg.validate_connection()

    def test_validate_row_custom_valid(self) -> None:
        cfg = Configuration(
            **{
                "connection": {
                    "db_host": "h",
                    "db_service_name": "s",
                    "#db_user": "u",
                    "#db_password": "p",
                },
                "destination": {
                    "write_mode": "custom",
                    "target_table": "APPS.MY_TABLE",
                    "write_operation": "insert",
                },
            }
        )
        # Should not raise
        cfg.validate_row()

    def test_validate_row_upsert_with_pk_valid(self) -> None:
        cfg = Configuration(
            **{
                "connection": {
                    "db_host": "h",
                    "db_service_name": "s",
                    "#db_user": "u",
                    "#db_password": "p",
                },
                "destination": {
                    "write_mode": "custom",
                    "target_table": "APPS.MY_TABLE",
                    "write_operation": "upsert",
                    "primary_key": ["ID"],
                },
            }
        )
        # Should not raise
        cfg.validate_row()


class TestColumnMapping:
    """Test the static column mapping logic from Component."""

    @staticmethod
    def _apply_mapping(rows: list[dict], mapping: dict[str, str]) -> list[dict]:
        """Mirror of Component._apply_mapping for unit testing."""
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

    def test_no_mapping_passthrough(self) -> None:
        rows = [{"COL_A": "1", "COL_B": "2"}]
        result = self._apply_mapping(rows, {})
        assert result == rows

    def test_rename_columns(self) -> None:
        rows = [{"INV_ID": "100", "INV_NUM": "INV-001"}]
        mapping = {"INV_ID": "INVOICE_ID", "INV_NUM": "INVOICE_NUM"}
        result = self._apply_mapping(rows, mapping)
        assert result[0]["INVOICE_ID"] == "100"
        assert result[0]["INVOICE_NUM"] == "INV-001"
        assert "INV_ID" not in result[0]

    def test_partial_mapping(self) -> None:
        rows = [{"INV_ID": "100", "AMOUNT": "500"}]
        mapping = {"INV_ID": "INVOICE_ID"}
        result = self._apply_mapping(rows, mapping)
        assert result[0]["INVOICE_ID"] == "100"
        assert result[0]["AMOUNT"] == "500"


class TestReadInputTable:
    """Test CSV reading and column uppercasing."""

    @staticmethod
    def _read_input_table(path: str) -> tuple[list[dict], list[str]]:
        """Mirror of Component._read_input_table for unit testing."""
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

    def test_reads_csv_and_uppercases_columns(self) -> None:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False, newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["invoice_id", "amount", "description"])
            writer.writerow(["1", "100.50", "Test invoice"])
            writer.writerow(["2", "200.00", "Another one"])
            path = f.name

        rows, columns = self._read_input_table(path)
        assert columns == ["INVOICE_ID", "AMOUNT", "DESCRIPTION"]
        assert len(rows) == 2
        assert rows[0]["INVOICE_ID"] == "1"
        assert rows[0]["AMOUNT"] == "100.50"

    def test_empty_values_become_none(self) -> None:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False, newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["id", "name"])
            writer.writerow(["1", ""])
            path = f.name

        rows, _ = self._read_input_table(path)
        assert rows[0]["NAME"] is None

    def test_empty_file_returns_empty(self) -> None:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False, newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["id", "name"])
            path = f.name

        rows, columns = self._read_input_table(path)
        assert rows == []
        assert columns == ["ID", "NAME"]
