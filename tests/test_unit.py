"""Unit tests for pure functions: presets, configuration, mapping, CSV reading."""

import csv
import tempfile

import pytest
from keboola.component.exceptions import UserException

from component import _apply_mapping, _collect_columns, _read_input_table
from configuration import Configuration, WriteMode, WriteOperation
from ebs_presets import get_entity_list, get_module_list, get_preset

# ── Preset Tests ──────────────────────────────────────────────────


class TestModuleList:
    def test_returns_all_modules(self) -> None:
        modules = get_module_list()
        keys = {m["value"] for m in modules}
        assert {"ap", "ar", "gl", "po", "inv", "om", "tca"} == keys

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

    def test_tca_entities(self) -> None:
        entities = get_entity_list("tca")
        keys = {e["value"] for e in entities}
        assert "hz_create_organization" in keys
        assert "hz_create_person" in keys
        assert "hz_create_cust_account" in keys

    def test_unknown_module_returns_empty(self) -> None:
        assert get_entity_list("nonexistent") == []


class TestGetPreset:
    def test_interface_table_preset(self) -> None:
        preset = get_preset("ap", "ap_invoices_interface")
        assert preset is not None
        assert preset["write_method"] == "interface_table"
        assert preset["target_table"] == "APPS.AP_INVOICES_INTERFACE"
        assert preset["concurrent_program"]["program"] == "APXIIMPT"

    def test_rest_api_preset(self) -> None:
        preset = get_preset("tca", "hz_create_organization")
        assert preset is not None
        assert preset["write_method"] == "rest_api"
        assert preset["rest_service"] == "create_organization"
        assert "ORGANIZATION_NAME" in preset["required_columns"]

    def test_gl_interface_has_defaults(self) -> None:
        preset = get_preset("gl", "gl_interface")
        assert preset is not None
        assert preset["default_values"]["STATUS"] == "NEW"
        assert preset["default_values"]["ACTUAL_FLAG"] == "A"
        assert preset["default_values"]["USER_JE_SOURCE_NAME"] == "Keboola"

    def test_unknown_preset_returns_none(self) -> None:
        assert get_preset("ap", "nonexistent") is None


# ── Configuration Tests ───────────────────────────────────────────


class TestConfiguration:
    def test_flatten_connection(self) -> None:
        cfg = Configuration(
            **{
                "connection": {
                    "db_host": "localhost",
                    "db_port": 1521,
                    "db_service_name": "EBSDB",
                    "db_user": "user",
                    "#db_password": "pass",
                },
                "destination": {"write_mode": "preset", "module": "ap", "entity": "ap_invoices_interface"},
            }
        )
        assert cfg.db_host == "localhost"
        assert cfg.db_user == "user"

    def test_defaults(self) -> None:
        cfg = Configuration()
        assert cfg.db_port == 1521
        assert cfg.destination.write_mode == WriteMode.PRESET
        assert cfg.destination.write_operation == WriteOperation.INSERT
        assert cfg.destination.batch_size == 1000
        assert cfg.destination.submit_program is True
        assert cfg.default_org_id == 204

    def test_validate_row_preset_missing_module(self) -> None:
        cfg = _make_config(destination={"write_mode": "preset", "module": "", "entity": ""})
        with pytest.raises(UserException, match="module"):
            cfg.validate_row()

    def test_validate_row_custom_missing_table(self) -> None:
        cfg = _make_config(destination={"write_mode": "custom", "target_table": ""})
        with pytest.raises(UserException, match="target_table"):
            cfg.validate_row()

    def test_validate_row_upsert_missing_pk(self) -> None:
        cfg = _make_config(
            destination={"write_mode": "custom", "target_table": "T", "write_operation": "upsert", "primary_key": []}
        )
        with pytest.raises(UserException, match="primary_key"):
            cfg.validate_row()

    def test_validate_connection_missing_host(self) -> None:
        cfg = Configuration()
        with pytest.raises(UserException, match="db_host"):
            cfg.validate_connection()

    def test_validate_rest_connection_missing_url(self) -> None:
        cfg = Configuration()
        with pytest.raises(UserException, match="rest_url"):
            cfg.validate_rest_connection()

    def test_validate_row_custom_valid(self) -> None:
        cfg = _make_config(destination={"write_mode": "custom", "target_table": "APPS.T", "write_operation": "insert"})
        cfg.validate_row()  # should not raise

    def test_validate_row_upsert_with_pk_valid(self) -> None:
        cfg = _make_config(
            destination={
                "write_mode": "custom",
                "target_table": "T",
                "write_operation": "upsert",
                "primary_key": ["ID"],
            }
        )
        cfg.validate_row()  # should not raise


# ── Column Mapping Tests ──────────────────────────────────────────


class TestColumnMapping:
    def test_no_mapping_passthrough(self) -> None:
        rows = [{"COL_A": "1", "COL_B": "2"}]
        assert _apply_mapping(rows, {}) == rows

    def test_rename_columns(self) -> None:
        rows = [{"INV_ID": "100", "INV_NUM": "INV-001"}]
        result = _apply_mapping(rows, {"INV_ID": "INVOICE_ID", "INV_NUM": "INVOICE_NUM"})
        assert result[0]["INVOICE_ID"] == "100"
        assert "INV_ID" not in result[0]

    def test_partial_mapping(self) -> None:
        rows = [{"INV_ID": "100", "AMOUNT": "500"}]
        result = _apply_mapping(rows, {"INV_ID": "INVOICE_ID"})
        assert result[0]["INVOICE_ID"] == "100"
        assert result[0]["AMOUNT"] == "500"


class TestCollectColumns:
    def test_collects_non_none(self) -> None:
        rows = [{"A": "1", "B": None, "C": "3"}, {"A": "2", "B": "4", "C": None}]
        assert _collect_columns(rows) == {"A", "B", "C"}

    def test_empty_rows(self) -> None:
        assert _collect_columns([]) == set()


# ── CSV Reading Tests ─────────────────────────────────────────────


class TestReadInputTable:
    def test_reads_and_uppercases(self) -> None:
        path = _write_csv(["invoice_id", "amount"], [["1", "100.50"], ["2", "200"]])
        rows, columns = _read_input_table(path)
        assert columns == ["INVOICE_ID", "AMOUNT"]
        assert len(rows) == 2
        assert rows[0]["INVOICE_ID"] == "1"

    def test_empty_values_become_none(self) -> None:
        path = _write_csv(["id", "name"], [["1", ""]])
        rows, _ = _read_input_table(path)
        assert rows[0]["NAME"] is None

    def test_empty_file(self) -> None:
        path = _write_csv(["id", "name"], [])
        rows, columns = _read_input_table(path)
        assert rows == []
        assert columns == ["ID", "NAME"]


# ── Helpers ───────────────────────────────────────────────────────


def _make_config(**overrides: dict) -> Configuration:
    base = {
        "connection": {"db_host": "h", "db_service_name": "s", "db_user": "u", "#db_password": "p"},
    }
    base.update(overrides)
    return Configuration(**base)


def _write_csv(headers: list[str], rows: list[list[str]]) -> str:
    with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False, newline="") as f:
        writer = csv.writer(f)
        writer.writerow(headers)
        for row in rows:
            writer.writerow(row)
        return f.name
