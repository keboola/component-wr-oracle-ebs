"""Oracle EBS ISG REST API client for direct entity writes.

Used for TCA (parties, customer accounts, sites) and AR (invoices)
where Oracle provides synchronous REST endpoints via Integrated SOA Gateway.

Base URL: http://<host>:8000/webservices/rest/
Auth: HTTP Basic (typically SYSADMIN user)
"""

import json
import logging

import requests
from keboola.component.exceptions import UserException


class EBSRestClient:
    def __init__(self, base_url: str, user: str, password: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.auth = (user, password)
        self.session.headers.update({"Content-Type": "application/json", "Accept": "application/json"})

    def test_connection(self) -> dict[str, str]:
        """Verify REST API is reachable by hitting the base URL."""
        try:
            resp = self.session.get(f"{self.base_url}/", timeout=30)
            return {"status": "ok", "http_status": str(resp.status_code), "url": self.base_url}
        except requests.RequestException as e:
            raise UserException(f"Cannot reach EBS REST API at {self.base_url}: {e}") from e

    def call_service(self, endpoint: str, payload: dict) -> dict:
        """Call an ISG REST service and return the parsed response.

        Raises UserException on HTTP errors or EBS API errors (X_RETURN_STATUS != 'S').
        """
        url = f"{self.base_url}{endpoint}"
        logging.debug("POST %s", url)

        try:
            # Use data=json.dumps() instead of json= to avoid charset in Content-Type.
            # Oracle EBS ISG rejects 'application/json; charset=utf-8'.
            resp = self.session.post(url, data=json.dumps(payload), timeout=120)
        except requests.RequestException as e:
            raise UserException(f"EBS REST call failed: {e}") from e

        if resp.status_code >= 400:
            raise UserException(f"EBS REST API error (HTTP {resp.status_code}): {resp.text[:500]}")

        data = resp.json()

        # Check for ISG service fault
        if "ISGServiceFault" in str(data):
            raise UserException(f"EBS ISG service fault: {data}")

        # Check X_RETURN_STATUS in OutputParameters
        output = data.get("OutputParameters", data)
        status = output.get("X_RETURN_STATUS", "")
        if status in ("E", "U"):
            msg = output.get("X_MSG_DATA")
            msg_count = output.get("X_MSG_COUNT", "0")
            if not msg:
                msg = f"EBS returned {msg_count} error message(s). Check EBS logs for details."
            prefix = "EBS API error" if status == "E" else "EBS unexpected error"
            raise UserException(f"{prefix}: {msg}")

        return data

    # ── TCA Operations ────────────────────────────────────────────

    def create_organization(self, org_rec: dict) -> dict:
        """Create a TCA organization (party)."""
        payload = {
            "InputParameters": {
                "P_INIT_MSG_LIST": "T",
                "P_ORGANIZATION_REC": {
                    "CREATED_BY_MODULE": "TCA_V2_API",
                    **org_rec,
                },
            }
        }
        return self.call_service("/hzparty/create_organization/", payload)

    def create_person(self, person_rec: dict) -> dict:
        """Create a TCA person (party)."""
        payload = {
            "InputParameters": {
                "P_INIT_MSG_LIST": "T",
                "P_PERSON_REC": {
                    "CREATED_BY_MODULE": "TCA_V2_API",
                    **person_rec,
                },
            }
        }
        return self.call_service("/hzparty/create_person/", payload)

    def create_cust_account(self, account_rec: dict, person_rec: dict | None = None) -> dict:
        """Create a customer account."""
        payload = {
            "InputParameters": {
                "P_INIT_MSG_LIST": "T",
                "P_CUST_ACCOUNT_REC": {
                    "CREATED_BY_MODULE": "TCA_V2_API",
                    **account_rec,
                },
                "P_PERSON_REC": {
                    "CREATED_BY_MODULE": "TCA_V2_API",
                    **(person_rec or {}),
                },
                "P_CREATE_PROFILE_AMT": "F",
            }
        }
        return self.call_service("/hzcustaccount/create_cust_account/", payload)

    def create_party_site(self, party_site_rec: dict, location_rec: dict) -> dict:
        """Create a party site with location."""
        payload = {
            "InputParameters": {
                "P_INIT_MSG_LIST": "T",
                "P_PARTY_SITE_REC": {
                    "CREATED_BY_MODULE": "TCA_V2_API",
                    **party_site_rec,
                },
                "P_LOCATION_REC": {
                    **location_rec,
                },
            }
        }
        return self.call_service("/hzpartysite/create_party_site/", payload)

    # ── AR Operations ─────────────────────────────────────────────

    def create_ar_invoice(self, header_rec: dict, lines: list[dict], org_id: int = 204) -> dict:
        """Create an AR invoice via ISG REST API."""
        line_items = []
        for i, line in enumerate(lines, 1):
            line_item = {
                "TRX_HEADER_ID": -1,
                "TRX_LINE_ID": -i,
                "LINE_NUMBER": i,
                "LINE_TYPE": "LINE",
                **line,
            }
            line_items.append(line_item)

        payload = {
            "CREATE_INVOICE_Input": {
                "RESTHeader": {
                    "Responsibility": "RECEIVABLES_MANAGER",
                    "RespApplication": "AR",
                    "SecurityGroup": "STANDARD",
                    "NLSLanguage": "AMERICAN",
                    "Org_Id": str(org_id),
                },
                "InputParameters": {
                    "P_API_VERSION": 1.0,
                    "P_INIT_MSG_LIST": "T",
                    "P_COMMIT": "T",
                    "P_BATCH_SOURCE_REC": header_rec.get("P_BATCH_SOURCE_REC", {"BATCH_SOURCE_ID": 3021}),
                    "P_TRX_HEADER_TBL": {
                        "P_TRX_HEADER_TBL_ITEM": {
                            "TRX_HEADER_ID": -1,
                            **{k: v for k, v in header_rec.items() if k != "P_BATCH_SOURCE_REC"},
                        }
                    },
                    "P_TRX_LINES_TBL": {
                        "P_TRX_LINES_TBL_ITEM": line_items if len(line_items) > 1 else line_items[0],
                    },
                    # CRITICAL: empty collections must be [] not null (ORA-06531)
                    "P_TRX_DIST_TBL": {"P_TRX_DIST_TBL_ITEM": []},
                    "P_TRX_SALESCREDITS_TBL": {"P_TRX_SALESCREDITS_TBL_ITEM": []},
                },
            }
        }
        return self.call_service("/arinvoice/create_invoice/", payload)
