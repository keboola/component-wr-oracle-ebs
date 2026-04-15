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

## Supported EBS Modules (Interface Tables)

- **AP** — Accounts Payable (`AP_INVOICES_INTERFACE`, `AP_INVOICE_LINES_INTERFACE`)
- **AR** — Accounts Receivable (`RA_INTERFACE_LINES_ALL`)
- **GL** — General Ledger (`GL_INTERFACE`)
- **PO** — Purchasing (`PO_HEADERS_INTERFACE`, `PO_LINES_INTERFACE`)
- **INV** — Inventory (`MTL_TRANSACTIONS_INTERFACE`)
- **OM** — Order Management (`OE_HEADERS_IFACE_ALL`, `OE_LINES_IFACE_ALL`)

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
