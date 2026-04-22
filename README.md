# Oracle E-Business Suite Writer

Write data to Oracle E-Business Suite via direct database connection using the `oracledb` thin driver.

## Features

| Feature | Description |
|---|---|
| Preset Mode | Select EBS module and interface table from dropdowns — component handles column validation and defaults |
| Custom Mode | Write to any Oracle table with full flexibility |
| INSERT / UPSERT | Simple insert or MERGE (update existing + insert new) by primary key |
| Column Mapping | Rename input columns to match Oracle target columns |
| Multi-Org Support | Automatically inject `ORG_ID` for multi-org interface tables |
| GL Ledger Support | Automatically inject `LEDGER_ID` for General Ledger interface |
| Sync Actions | Test connection, list operating units, list ledgers, list modules, list entities, list target columns |

## Requirements

The component ships with Oracle Instant Client Basic Light (~50 MB) embedded in the Docker image so it connects out of the box against any supported Oracle EBS version — including older installations that only advertise the legacy 10G password verifier or run with `sec_case_sensitive_logon = FALSE`. No Oracle client installation is required on customer infrastructure; the component speaks to the EBS database directly.

Internally, `python-oracledb` runs in **thick mode** when the bundled Instant Client is loadable, and falls back to **thin mode** otherwise. Thick mode transparently handles the full range of Oracle authentication protocols (10G, 11G, 12C).

Oracle Instant Client is distributed under the [Oracle Free Use Terms and Conditions (FUTC)](https://www.oracle.com/downloads/licenses/oracle-free-license.html) and is freely redistributable in third-party applications.

### Database user permissions

The configured DB user needs:

- `CREATE SESSION`
- `SELECT` on `apps.hr_operating_units`, `apps.gl_ledgers`, `apps.fnd_concurrent_requests`, `sys.all_tables`, `sys.all_tab_columns`
- `INSERT` on each target interface table (e.g. `apps.ap_invoices_interface`, `apps.po_headers_interface`, `apps.mtl_transactions_interface`)
- `EXECUTE` on `apps.fnd_request` (to submit concurrent programs) and `apps.fnd_global` (to initialize responsibility context)

### ISG REST API (for TCA / AR writes)

For preset modes that use the ISG REST API (`tca.hz_create_organization`, `tca.hz_create_person`, `tca.hz_create_cust_account`, `ar.ar_invoice_create`), a separate ISG-enabled EBS responsibility is required — typically `SYSADMIN` or a custom responsibility with access to the `hzparty`, `hzcustaccount`, `hzpartysite`, and `arinvoice` services at `http://<host>:8000/webservices/rest/`.

## Supported EBS Modules

### Interface-table writes (3-step: INSERT → submit program → check status)

- **AP** — Accounts Payable (`AP_INVOICES_INTERFACE`, `AP_INVOICE_LINES_INTERFACE`)
- **PO** — Purchasing (`PO_HEADERS_INTERFACE`, `PO_LINES_INTERFACE`)
- **INV** — Inventory (`MTL_TRANSACTIONS_INTERFACE`)
- **OM** — Order Management (`OE_HEADERS_IFACE_ALL`, `OE_LINES_IFACE_ALL`)
- **GL** — General Ledger (`GL_INTERFACE`) — *requires Journal Import fixtures to be fully installed; some Vision Demo appliances ship with the editioning view present but the base table missing*

### REST API writes (synchronous ISG calls)

- **TCA** — Create Organization / Create Person / Create Customer Account
- **AR** — Create Invoice (`ar_invoice_create`)

## Configuration

### Connection Settings (configSchema)

| Parameter | Description |
|---|---|
| `db_host` | Oracle database server hostname or IP |
| `db_port` | Port (default: 1521) |
| `db_service_name` | Oracle service name (e.g. `ebs_EBSDB`) |
| `#db_user` | Database username (encrypted) |
| `#db_password` | Database password (encrypted) |

### Write Row (configRowSchema)

| Parameter | Description |
|---|---|
| `write_mode` | `preset` (guided) or `custom` (any table) |
| `module` | EBS module code (preset mode) |
| `entity` | Interface table entity (preset mode) |
| `org_id` | Operating Unit ID for multi-org tables |
| `ledger_id` | Ledger ID for GL interface |
| `target_table` | Full Oracle table name (custom mode) |
| `write_operation` | `insert` or `upsert` (MERGE) |
| `primary_key` | Primary key columns (required for upsert) |
| `column_mapping` | Map input columns to target columns |
| `batch_size` | Rows per batch (default: 1000) |

## Development

```bash
git clone https://github.com/keboola/component-wr-oracle-ebs
cd component-wr-oracle-ebs
docker-compose build
docker-compose run --rm dev
```

### Run tests

```bash
docker-compose run --rm test
```

## Deployment

See [Keboola Developer Documentation](https://developers.keboola.com/extend/component/deployment/).
