Oracle E-Business Suite (EBS) writer loads data from Keboola Storage into your EBS instance using the two standard EBS integration patterns: interface tables + concurrent programs, and ISG REST APIs.

**Two write paths:**

- **Interface tables (JDBC)** — bulk writes for transactional data. The component inserts rows into the `*_INTERFACE` staging tables, submits the corresponding EBS concurrent program via `FND_REQUEST.SUBMIT_REQUEST`, and polls for completion. Supported: AP Invoices / Invoice Lines, PO Headers / Lines, INV Transactions, OM Order Headers / Lines, GL Journal Interface.
- **ISG REST API** — synchronous per-row writes for master-data entities. Supported: TCA Create Organization / Create Person / Create Customer Account, AR Create Invoice.

**Features:**

- Preset mode: guided UI with dropdowns for module, target entity, operating unit, and ledger. Required and optional columns are pre-declared per preset.
- Custom mode: write into any Oracle table for full flexibility.
- Write operations: `INSERT` and `UPSERT` (Oracle `MERGE` by primary key).
- Column mapping: rename input CSV columns to Oracle target columns.
- Automatic enrichment: default values, multi-org `ORG_ID` injection, GL `LEDGER_ID` injection, sequence-generated primary keys (e.g. `APPS.AP_INVOICES_INTERFACE_S.NEXTVAL`).
- Batched writes with configurable batch size.
- Sync actions: test connection, list operating units, list ledgers, list modules, list target entities, list target columns.

**Oracle Instant Client bundled in-image** — the component ships with Oracle Instant Client Basic Light under the Oracle Free Use Terms and Conditions license. No Oracle client installation is required on customer infrastructure, and the component works out of the box against EBS installs that use the legacy 10G password verifier or `SEC_CASE_SENSITIVE_LOGON=FALSE` — scenarios where the pure-Python thin driver cannot authenticate.

**Supported modules:** Accounts Payable (AP), Accounts Receivable (AR), General Ledger (GL), Purchasing (PO), Inventory (INV), Order Management (OM), Trading Community Architecture (TCA).
