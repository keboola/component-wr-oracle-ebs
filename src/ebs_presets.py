"""Oracle EBS write-target presets per module/entity.

Two write methods:
  - interface_table: Bulk INSERT into staging table → submit concurrent program → check status
  - rest_api: Synchronous ISG REST API calls (one record per call)

Interface tables use Oracle's Open Interface pattern; concurrent programs
process the staging data into EBS base tables.
"""

from __future__ import annotations

MODULE_LABELS: dict[str, str] = {
    "ap": "Accounts Payable (AP)",
    "ar": "Accounts Receivable (AR)",
    "gl": "General Ledger (GL)",
    "po": "Purchasing (PO)",
    "inv": "Inventory (INV)",
    "om": "Order Management (OM)",
    "tca": "Trading Community (TCA)",
}

PRESETS: dict[str, dict[str, dict]] = {
    # ── AP — Interface Tables ─────────────────────────────────────
    "ap": {
        "ap_invoices_interface": {
            "name": "AP Invoices Interface",
            "write_method": "interface_table",
            "target_table": "APPS.AP_INVOICES_INTERFACE",
            "required_columns": [
                "INVOICE_NUM",
                "INVOICE_DATE",
                "VENDOR_ID",
                "INVOICE_AMOUNT",
                "INVOICE_CURRENCY_CODE",
            ],
            "optional_columns": [
                "INVOICE_TYPE_LOOKUP_CODE",
                "DESCRIPTION",
                "GL_DATE",
                "PAYMENT_METHOD_CODE",
                "TERMS_ID",
                "VENDOR_SITE_ID",
                "ORG_ID",
                "GROUP_ID",
            ],
            "default_primary_key": ["INVOICE_ID"],
            "default_values": {
                "SOURCE": "Keboola",
            },
            "sequence_columns": {
                "INVOICE_ID": "APPS.AP_INVOICES_INTERFACE_S",
            },
            "concurrent_program": {
                "application": "SQLAP",
                "program": "APXIIMPT",
                "description": "Payables Open Interface Import",
                # Positional args for APXIIMPT (Payables Open Interface Import) — 11 params used:
                # 1=Operating Unit (NULL → MOAC default), 2=Source (REQUIRED), 3=Group,
                # 4=Batch Name (REQUIRED), 5=Hold Name, 6=Hold Reason, 7=GL Date,
                # 8=Purge, 9=Trace Switch, 10=Debug Switch, 11=Summarize Report.
                # Source and Batch Name must be non-null; everything else can be NULL.
                "arguments": [None, "Keboola", None, "Keboola", None, None, None, "N", "N", "N", "N"],
                # FND session context for fnd_global.apps_initialize
                # user_id=0 → SYSADMIN; resp_id=20639 → Payables Manager (application_id=200)
                "fnd_context": {"user_id": 0, "resp_id": 20639, "resp_appl_id": 200},
            },
            "status_check": {
                "group_column": "GROUP_ID",
                "status_column": "STATUS",
                # Query that returns one row per rejected invoice with a REASON.
                # Joined to ap_interface_rejections (linked by parent_id=invoice_id)
                # and fnd_lookup_values for human-readable message text.
                "rejection_query": """
                    SELECT i.INVOICE_NUM,
                           i.VENDOR_ID,
                           i.STATUS,
                           NVL(l.MEANING, r.REJECT_LOOKUP_CODE) AS REASON
                      FROM apps.ap_invoices_interface i
                      LEFT JOIN apps.ap_interface_rejections r
                        ON r.PARENT_ID = i.INVOICE_ID
                       AND r.PARENT_TABLE = 'AP_INVOICES_INTERFACE'
                      LEFT JOIN apps.fnd_lookup_values l
                        ON l.LOOKUP_TYPE = 'NLS TRANSLATION'
                       AND l.LOOKUP_CODE = r.REJECT_LOOKUP_CODE
                       AND l.LANGUAGE = USERENV('LANG')
                     WHERE i.GROUP_ID = :group_id
                       AND i.STATUS = 'REJECTED'
                """,
            },
            "multi_org": True,
            "uses_ledger": False,
        },
        "ap_invoice_lines_interface": {
            "name": "AP Invoice Lines Interface",
            "write_method": "interface_table",
            "target_table": "APPS.AP_INVOICE_LINES_INTERFACE",
            "required_columns": [
                "INVOICE_ID",
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
            "sequence_columns": {
                "INVOICE_LINE_ID": "APPS.AP_INVOICE_LINES_INTERFACE_S",
            },
            "concurrent_program": None,  # processed by APXIIMPT on header
            "status_check": None,
            "multi_org": True,
            "uses_ledger": False,
        },
    },
    # ── AR — REST API ─────────────────────────────────────────────
    "ar": {
        "ar_invoice_create": {
            "name": "AR Invoice (Create via REST)",
            "write_method": "rest_api",
            "rest_service": "create_ar_invoice",
            "required_columns": [
                "BILL_TO_CUSTOMER_ID",
                "CUST_TRX_TYPE_ID",
                "TRX_DATE",
                "TRX_CURRENCY",
            ],
            "optional_columns": [
                "BILL_TO_SITE_USE_ID",
                "GL_DATE",
                "TERM_ID",
                "BATCH_SOURCE_ID",
                "QUANTITY_INVOICED",
                "UNIT_SELLING_PRICE",
                "LINE_DESCRIPTION",
            ],
            "default_primary_key": [],
            "default_values": {
                "BATCH_SOURCE_ID": "3021",
            },
            "response_id_field": "CUSTOMER_TRX_ID",
            "multi_org": True,
            "uses_ledger": False,
        },
    },
    # ── GL — Interface Tables ─────────────────────────────────────
    "gl": {
        "gl_interface": {
            "name": "GL Journal Interface",
            "write_method": "interface_table",
            "target_table": "APPS.GL_INTERFACE",
            "required_columns": [
                "SET_OF_BOOKS_ID",
                "ACCOUNTING_DATE",
                "CURRENCY_CODE",
                "USER_JE_CATEGORY_NAME",
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
                "USER_JE_SOURCE_NAME": "Keboola",
                "CREATED_BY": "0",
                "DATE_CREATED": "SYSDATE",
            },
            "sequence_columns": {},
            "concurrent_program": {
                "application": "SQLGL",
                "program": "GLLEZL",
                "description": "Journal Import",
                # GLLEZL params (look up via $SRS$.GLLEZL): all 6 of these are REQUIRED.
                # 1=Data Access Set ID, 2=Source ('Keboola'), 3=Ledger (from row config),
                # 4=Group ID, 5=Post Errors to Suspense, 6=Create Summary Journals,
                # 7=Import Descriptive Flexfields. Real values must be patched in by
                # the caller — placeholder defaults below will be rejected by the program.
                # TODO: wire ledger_id and data_access_set_id from configuration.
                "arguments": [None, "Keboola", None, None, "N", "N", "N"],
                # user_id=0 → SYSADMIN; resp_id=20434 → General Ledger Super User (application_id=101)
                "fnd_context": {"user_id": 0, "resp_id": 20434, "resp_appl_id": 101},
            },
            "status_check": {
                "group_column": "GROUP_ID",
                "status_column": "STATUS",
            },
            "multi_org": False,
            "uses_ledger": True,
        },
    },
    # ── PO — Interface Tables ─────────────────────────────────────
    "po": {
        "po_headers_interface": {
            "name": "PO Headers Interface",
            "write_method": "interface_table",
            "target_table": "APPS.PO_HEADERS_INTERFACE",
            "required_columns": [
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
                "PROCESS_CODE": "PENDING",
                "ACTION": "ORIGINAL",
                "DOCUMENT_TYPE_CODE": "STANDARD",
            },
            "sequence_columns": {
                "INTERFACE_HEADER_ID": "APPS.PO_HEADERS_INTERFACE_S",
            },
            "concurrent_program": {
                "application": "PO",
                "program": "POXPOPDOI",
                "description": "Import Standard Purchase Orders",
                # POXPOPDOI params (look up via $SRS$.POXPOPDOI): 18 positional, 3 REQUIRED.
                # 1=p_buyer_id, 2=p_document_type, 3=p_document_subtype,
                # 4=p_create_items (REQUIRED 'Y'/'N'), 5=p_create_sourcing_rules_flag,
                # 6=p_approved_status (REQUIRED — typically 'INCOMPLETE' or 'APPROVED'),
                # 7=p_rel_gen_metho, 8=p_selected_batch_id, 9=p_org_id (from row config),
                # 10..15 sourcing/CLM flags, 16=p_clm_flag (REQUIRED 'Y'/'N'),
                # 17=p_batch_size, 18=p_gather_stats.
                "arguments": [
                    None,
                    "STANDARD",
                    "STANDARD",
                    "N",
                    None,
                    "INCOMPLETE",
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    "N",
                    None,
                    None,
                ],
                # user_id=0 → SYSADMIN; resp_id=20707 → Purchasing Super User (application_id=201)
                "fnd_context": {"user_id": 0, "resp_id": 20707, "resp_appl_id": 201},
            },
            "status_check": {
                "group_column": "BATCH_ID",
                "status_column": "PROCESS_CODE",
            },
            "multi_org": True,
            "uses_ledger": False,
        },
        "po_lines_interface": {
            "name": "PO Lines Interface",
            "write_method": "interface_table",
            "target_table": "APPS.PO_LINES_INTERFACE",
            "required_columns": [
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
                "LINE_TYPE_ID",
            ],
            "default_primary_key": ["INTERFACE_LINE_ID"],
            "default_values": {},
            "sequence_columns": {
                "INTERFACE_LINE_ID": "APPS.PO_LINES_INTERFACE_S",
            },
            "concurrent_program": None,  # processed by POXPOPDOI on header
            "status_check": None,
            "multi_org": False,
            "uses_ledger": False,
        },
    },
    # ── INV — Interface Tables ────────────────────────────────────
    "inv": {
        "mtl_transactions_interface": {
            "name": "Inventory Transactions Interface",
            "write_method": "interface_table",
            "target_table": "APPS.MTL_TRANSACTIONS_INTERFACE",
            "required_columns": [
                "TRANSACTION_TYPE_ID",
                "INVENTORY_ITEM_ID",
                "ORGANIZATION_ID",
                "TRANSACTION_QUANTITY",
                "TRANSACTION_UOM",
                "TRANSACTION_DATE",
            ],
            "optional_columns": [
                "SUBINVENTORY_CODE",
                "LOCATOR_ID",
                "TRANSFER_ORGANIZATION",
                "TRANSFER_SUBINVENTORY",
                "REASON_ID",
                "SOURCE_HEADER_ID",
                "SOURCE_LINE_ID",
            ],
            "default_primary_key": ["TRANSACTION_INTERFACE_ID"],
            "default_values": {
                "SOURCE_CODE": "Keboola",
                "PROCESS_FLAG": "1",
                "TRANSACTION_MODE": "3",
            },
            "sequence_columns": {
                "TRANSACTION_INTERFACE_ID": "APPS.MTL_MATERIAL_TRANSACTIONS_S",
            },
            "concurrent_program": {
                "application": "INV",
                "program": "INCTCM",
                "description": "Process transaction interface",
                "arguments": ["", "", "", "", ""],
            },
            "status_check": {
                "group_column": "SOURCE_CODE",
                "status_column": "PROCESS_FLAG",
            },
            # MTL_TRANSACTIONS_INTERFACE uses ORGANIZATION_ID (provided in required_columns),
            # not ORG_ID. Set multi_org=False so the component doesn't inject ORG_ID.
            "multi_org": False,
            "uses_ledger": False,
        },
    },
    # ── OM — Interface Tables ─────────────────────────────────────
    "om": {
        "oe_headers_iface": {
            "name": "Sales Order Headers Interface",
            "write_method": "interface_table",
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
            ],
            "default_primary_key": ["ORIG_SYS_DOCUMENT_REF"],
            "default_values": {
                "OPERATION_CODE": "CREATE",
            },
            "sequence_columns": {},
            "concurrent_program": {
                "application": "ONT",
                "program": "OEOIMP",
                "description": "Order Import",
                "arguments": ["", "", "", "", "", "", "", "", "", ""],
            },
            "status_check": None,
            "multi_org": True,
            "uses_ledger": False,
        },
    },
    # ── TCA — REST API ────────────────────────────────────────────
    "tca": {
        "hz_create_organization": {
            "name": "Create Organization (TCA Party)",
            "write_method": "rest_api",
            "rest_service": "create_organization",
            "required_columns": ["ORGANIZATION_NAME"],
            "optional_columns": [
                "DUNS_NUMBER_C",
                "JGZZ_FISCAL_CODE",
                "TAX_REFERENCE",
                "SIC_CODE",
                "CATEGORY_CODE",
                "URL",
            ],
            "default_primary_key": [],
            "default_values": {
                "CREATED_BY_MODULE": "TCA_V1_API",
            },
            "response_id_field": "X_PARTY_ID",
            "multi_org": False,
            "uses_ledger": False,
        },
        "hz_create_person": {
            "name": "Create Person (TCA Party)",
            "write_method": "rest_api",
            "rest_service": "create_person",
            "required_columns": ["PERSON_FIRST_NAME", "PERSON_LAST_NAME"],
            "optional_columns": [
                "PERSON_MIDDLE_NAME",
                "PERSON_TITLE",
                "PERSON_NAME_SUFFIX",
                "KNOWN_AS",
            ],
            "default_primary_key": [],
            "default_values": {
                "CREATED_BY_MODULE": "TCA_V1_API",
            },
            "response_id_field": "X_PARTY_ID",
            "multi_org": False,
            "uses_ledger": False,
        },
        "hz_create_cust_account": {
            "name": "Create Customer Account",
            "write_method": "rest_api",
            "rest_service": "create_cust_account",
            "required_columns": ["ACCOUNT_NAME"],
            "optional_columns": [
                "ACCOUNT_NUMBER",
                "CUSTOMER_CLASS_CODE",
                "PERSON_FIRST_NAME",
                "PERSON_LAST_NAME",
            ],
            "default_primary_key": [],
            "default_values": {
                "CREATED_BY_MODULE": "TCA_V1_API",
            },
            "response_id_field": "X_CUST_ACCOUNT_ID",
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
