"""Oracle EBS write-target presets per module/entity.

Each preset defines the target interface or staging table used by Oracle EBS
for data import, along with required columns and optional org/ledger awareness.

Oracle EBS uses **interface tables** as the standard mechanism for loading data.
Concurrent programs then process these staging rows into the base tables.
"""

from __future__ import annotations

MODULE_LABELS: dict[str, str] = {
    "ap": "Accounts Payable (AP)",
    "ar": "Accounts Receivable (AR)",
    "gl": "General Ledger (GL)",
    "po": "Purchasing (PO)",
    "inv": "Inventory (INV)",
    "om": "Order Management (OM)",
}

PRESETS: dict[str, dict[str, dict]] = {
    "ap": {
        "ap_invoices_interface": {
            "name": "AP Invoices Interface",
            "target_table": "APPS.AP_INVOICES_INTERFACE",
            "required_columns": [
                "INVOICE_ID",
                "INVOICE_NUM",
                "INVOICE_DATE",
                "VENDOR_ID",
                "INVOICE_AMOUNT",
                "INVOICE_CURRENCY_CODE",
                "SOURCE",
            ],
            "optional_columns": [
                "DESCRIPTION",
                "INVOICE_TYPE_LOOKUP_CODE",
                "GL_DATE",
                "PAYMENT_METHOD_CODE",
                "PAY_GROUP_LOOKUP_CODE",
                "TERMS_ID",
                "ORG_ID",
            ],
            "default_primary_key": ["INVOICE_ID"],
            "default_values": {
                "SOURCE": "KEBOOLA",
            },
            "multi_org": True,
            "uses_ledger": False,
        },
        "ap_invoice_lines_interface": {
            "name": "AP Invoice Lines Interface",
            "target_table": "APPS.AP_INVOICE_LINES_INTERFACE",
            "required_columns": [
                "INVOICE_ID",
                "INVOICE_LINE_ID",
                "LINE_NUMBER",
                "LINE_TYPE_LOOKUP_CODE",
                "AMOUNT",
            ],
            "optional_columns": [
                "DESCRIPTION",
                "DIST_CODE_COMBINATION_ID",
                "QUANTITY_INVOICED",
                "UNIT_PRICE",
                "ORG_ID",
            ],
            "default_primary_key": ["INVOICE_ID", "INVOICE_LINE_ID"],
            "default_values": {},
            "multi_org": True,
            "uses_ledger": False,
        },
    },
    "ar": {
        "ra_interface_lines": {
            "name": "AR Interface Lines",
            "target_table": "APPS.RA_INTERFACE_LINES_ALL",
            "required_columns": [
                "INTERFACE_LINE_ID",
                "INTERFACE_LINE_CONTEXT",
                "INTERFACE_LINE_ATTRIBUTE1",
                "BATCH_SOURCE_NAME",
                "LINE_TYPE",
                "AMOUNT",
                "CURRENCY_CODE",
            ],
            "optional_columns": [
                "DESCRIPTION",
                "ORIG_SYSTEM_BILL_CUSTOMER_ID",
                "ORIG_SYSTEM_BILL_ADDRESS_ID",
                "CUST_TRX_TYPE_ID",
                "GL_DATE",
                "TRX_DATE",
                "ORG_ID",
            ],
            "default_primary_key": ["INTERFACE_LINE_ID"],
            "default_values": {
                "BATCH_SOURCE_NAME": "KEBOOLA",
            },
            "multi_org": True,
            "uses_ledger": False,
        },
    },
    "gl": {
        "gl_interface": {
            "name": "GL Journal Interface",
            "target_table": "APPS.GL_INTERFACE",
            "required_columns": [
                "STATUS",
                "SET_OF_BOOKS_ID",
                "ACCOUNTING_DATE",
                "CURRENCY_CODE",
                "DATE_CREATED",
                "CREATED_BY",
                "ACTUAL_FLAG",
                "USER_JE_CATEGORY_NAME",
                "USER_JE_SOURCE_NAME",
                "ENTERED_DR",
                "ENTERED_CR",
            ],
            "optional_columns": [
                "SEGMENT1",
                "SEGMENT2",
                "SEGMENT3",
                "SEGMENT4",
                "SEGMENT5",
                "SEGMENT6",
                "CODE_COMBINATION_ID",
                "REFERENCE1",
                "REFERENCE4",
                "REFERENCE5",
                "REFERENCE10",
                "GROUP_ID",
                "LEDGER_ID",
            ],
            "default_primary_key": [],
            "default_values": {
                "STATUS": "NEW",
                "ACTUAL_FLAG": "A",
                "USER_JE_SOURCE_NAME": "KEBOOLA",
            },
            "multi_org": False,
            "uses_ledger": True,
        },
    },
    "po": {
        "po_headers_interface": {
            "name": "PO Headers Interface",
            "target_table": "APPS.PO_HEADERS_INTERFACE",
            "required_columns": [
                "INTERFACE_HEADER_ID",
                "ACTION",
                "DOCUMENT_TYPE_CODE",
                "VENDOR_ID",
                "ORG_ID",
            ],
            "optional_columns": [
                "VENDOR_SITE_ID",
                "CURRENCY_CODE",
                "AGENT_ID",
                "SHIP_TO_LOCATION_ID",
                "BILL_TO_LOCATION_ID",
                "BATCH_ID",
                "APPROVAL_STATUS",
            ],
            "default_primary_key": ["INTERFACE_HEADER_ID"],
            "default_values": {
                "ACTION": "ORIGINAL",
                "DOCUMENT_TYPE_CODE": "STANDARD",
            },
            "multi_org": True,
            "uses_ledger": False,
        },
        "po_lines_interface": {
            "name": "PO Lines Interface",
            "target_table": "APPS.PO_LINES_INTERFACE",
            "required_columns": [
                "INTERFACE_LINE_ID",
                "INTERFACE_HEADER_ID",
                "LINE_NUM",
                "ITEM_DESCRIPTION",
                "QUANTITY",
                "UNIT_PRICE",
            ],
            "optional_columns": [
                "ITEM_ID",
                "UNIT_OF_MEASURE",
                "CATEGORY_ID",
                "SHIP_TO_ORGANIZATION_ID",
                "SHIP_TO_LOCATION_ID",
                "LINE_TYPE_ID",
            ],
            "default_primary_key": ["INTERFACE_LINE_ID"],
            "default_values": {},
            "multi_org": False,
            "uses_ledger": False,
        },
    },
    "inv": {
        "mtl_transactions_interface": {
            "name": "Inventory Transactions Interface",
            "target_table": "APPS.MTL_TRANSACTIONS_INTERFACE",
            "required_columns": [
                "TRANSACTION_INTERFACE_ID",
                "TRANSACTION_TYPE_ID",
                "INVENTORY_ITEM_ID",
                "ORGANIZATION_ID",
                "TRANSACTION_QUANTITY",
                "TRANSACTION_UOM",
                "TRANSACTION_DATE",
                "SOURCE_CODE",
                "SOURCE_LINE_ID",
                "SOURCE_HEADER_ID",
                "PROCESS_FLAG",
                "TRANSACTION_MODE",
            ],
            "optional_columns": [
                "SUBINVENTORY_CODE",
                "LOCATOR_ID",
                "TRANSFER_ORGANIZATION",
                "TRANSFER_SUBINVENTORY",
                "REASON_ID",
            ],
            "default_primary_key": ["TRANSACTION_INTERFACE_ID"],
            "default_values": {
                "SOURCE_CODE": "KEBOOLA",
                "PROCESS_FLAG": "1",
                "TRANSACTION_MODE": "3",
            },
            "multi_org": True,
            "uses_ledger": False,
        },
    },
    "om": {
        "oe_headers_iface": {
            "name": "Sales Order Headers Interface",
            "target_table": "APPS.OE_HEADERS_IFACE_ALL",
            "required_columns": [
                "ORDER_SOURCE_ID",
                "ORIG_SYS_DOCUMENT_REF",
                "OPERATION_CODE",
                "ORDER_TYPE_ID",
                "SOLD_TO_ORG_ID",
                "ORG_ID",
            ],
            "optional_columns": [
                "ORDERED_DATE",
                "TRANSACTIONAL_CURR_CODE",
                "PRICE_LIST_ID",
                "SHIP_FROM_ORG_ID",
                "SHIP_TO_ORG_ID",
                "INVOICE_TO_ORG_ID",
            ],
            "default_primary_key": ["ORIG_SYS_DOCUMENT_REF"],
            "default_values": {
                "OPERATION_CODE": "CREATE",
            },
            "multi_org": True,
            "uses_ledger": False,
        },
        "oe_lines_iface": {
            "name": "Sales Order Lines Interface",
            "target_table": "APPS.OE_LINES_IFACE_ALL",
            "required_columns": [
                "ORDER_SOURCE_ID",
                "ORIG_SYS_DOCUMENT_REF",
                "ORIG_SYS_LINE_REF",
                "OPERATION_CODE",
                "INVENTORY_ITEM_ID",
                "ORDERED_QUANTITY",
            ],
            "optional_columns": [
                "UNIT_SELLING_PRICE",
                "UNIT_LIST_PRICE",
                "SCHEDULE_SHIP_DATE",
                "SHIP_FROM_ORG_ID",
            ],
            "default_primary_key": ["ORIG_SYS_DOCUMENT_REF", "ORIG_SYS_LINE_REF"],
            "default_values": {
                "OPERATION_CODE": "CREATE",
            },
            "multi_org": False,
            "uses_ledger": False,
        },
    },
}


def get_module_list() -> list[dict[str, str]]:
    """Return available modules for UI dropdown."""
    return [{"value": k, "label": v} for k, v in MODULE_LABELS.items() if k in PRESETS]


def get_entity_list(module: str) -> list[dict[str, str]]:
    """Return available entities for a given module."""
    entities = PRESETS.get(module, {})
    return [{"value": k, "label": v["name"]} for k, v in entities.items()]


def get_preset(module: str, entity: str) -> dict | None:
    """Return a preset definition or None if not found."""
    return PRESETS.get(module, {}).get(entity)
