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
    def __init__(
        self,
        base_url: str,
        user: str,
        password: str,
        default_org_id: int | None = None,
        responsibility: str = "RECEIVABLES_MANAGER",
        resp_application: str = "AR",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.auth = (user, password)
        self.session.headers.update({"Content-Type": "application/json", "Accept": "application/json"})
        # MOAC context propagated to ISG via RESTHeader. Without it Oracle calls run with
        # Responsibility=null / Org_id=-1 and TCA/AR APIs reject with silent FND_MSG_PUB
        # errors. Receivables Manager is the standard Vision responsibility that has both
        # TCA party and AR invoice grants; can be overridden per-customer config later.
        self.default_org_id = default_org_id
        self.responsibility = responsibility
        self.resp_application = resp_application

    def _rest_header(self, org_id: int | None = None) -> dict[str, str]:
        return {
            "Responsibility": self.responsibility,
            "RespApplication": self.resp_application,
            "SecurityGroup": "STANDARD",
            "NLSLanguage": "AMERICAN",
            "Org_Id": str(org_id if org_id is not None else (self.default_org_id or 204)),
        }

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
            # When X_MSG_COUNT > 1, EBS often leaves X_MSG_DATA empty and expects
            # FND_MSG_PUB.GET to be called per-message. ISG doesn't expose that
            # via REST, and FND_MSG_PUB is session-local so a separate JDBC
            # connection can't retrieve it. Surface the full payload (often the
            # nested OutputParameters carry hints) plus an actionable pointer.
            if not msg:
                msg = (
                    f"EBS returned {msg_count} error message(s) but did not include them in the "
                    f"response (X_MSG_DATA is null — Oracle expects FND_MSG_PUB.GET per message, "
                    f"which ISG REST does not expose). Check $LOG_HOME/ora/10.1.3/oafm/<server>/"
                    f"oafm_server1.out on the EBS apps tier (or apps.fnd_log_messages if "
                    f"AFCORE_LOGGING_ENABLED='Y') for the actual validation errors. "
                    f"Full response: {json.dumps(data)[:1500]}"
                )
            prefix = "EBS API error" if status == "E" else "EBS unexpected error"
            raise UserException(f"{prefix}: {msg}")

        return data

    # ── TCA Operations ────────────────────────────────────────────

    def create_organization(self, org_rec: dict, org_id: int | None = None) -> dict:
        """Create a TCA organization (party).

        NOTE: Oracle EBS ISG is sensitive to JSON key order — user-provided fields
        MUST come before CREATED_BY_MODULE or the API returns X_RETURN_STATUS=E.
        Caller-supplied CREATED_BY_MODULE wins; default falls back to TCA_V2_API.
        """
        payload = {
            "InputParameters": {
                "RESTHeader": self._rest_header(org_id),
                "P_INIT_MSG_LIST": "T",
                "P_ORGANIZATION_REC": {
                    # Drop None / empty values — Oracle TCA validates JSON null
                    # against typed fields and rejects the call. Missing keys are fine.
                    **{k: v for k, v in org_rec.items() if k != "CREATED_BY_MODULE" and v not in (None, "")},
                    "CREATED_BY_MODULE": org_rec.get("CREATED_BY_MODULE", "TCA_V2_API"),
                },
            }
        }
        return self.call_service("/hzparty/create_organization/", payload)

    def create_person(self, person_rec: dict, org_id: int | None = None) -> dict:
        """Create a TCA person (party)."""
        payload = {
            "InputParameters": {
                "RESTHeader": self._rest_header(org_id),
                "P_INIT_MSG_LIST": "T",
                "P_PERSON_REC": {
                    **{k: v for k, v in person_rec.items() if k != "CREATED_BY_MODULE" and v not in (None, "")},
                    "CREATED_BY_MODULE": person_rec.get("CREATED_BY_MODULE", "TCA_V2_API"),
                },
            }
        }
        return self.call_service("/hzparty/create_person/", payload)

    def create_cust_account(self, account_rec: dict, person_rec: dict | None = None, org_id: int | None = None) -> dict:
        """Create a customer account."""
        person_rec = person_rec or {}
        payload = {
            "InputParameters": {
                "RESTHeader": self._rest_header(org_id),
                "P_INIT_MSG_LIST": "T",
                "P_CUST_ACCOUNT_REC": {
                    **{k: v for k, v in account_rec.items() if k != "CREATED_BY_MODULE"},
                    "CREATED_BY_MODULE": account_rec.get("CREATED_BY_MODULE", "TCA_V2_API"),
                },
                "P_PERSON_REC": {
                    **{k: v for k, v in person_rec.items() if k != "CREATED_BY_MODULE"},
                    "CREATED_BY_MODULE": person_rec.get("CREATED_BY_MODULE", "TCA_V2_API"),
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
                    **{k: v for k, v in party_site_rec.items() if k != "CREATED_BY_MODULE"},
                    "CREATED_BY_MODULE": party_site_rec.get("CREATED_BY_MODULE", "TCA_V2_API"),
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
