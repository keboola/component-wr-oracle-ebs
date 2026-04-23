"""Unit tests for EBSRestClient using mocked HTTP responses.

Covers the REST path that VCR tests 08 and 09 cover end-to-end. Useful when
the EBS apps middle tier (port 8000) is unavailable for live VCR recording.
"""

from unittest.mock import Mock, patch

import pytest
from keboola.component.exceptions import UserException

from ebs_rest_client import EBSRestClient


@pytest.fixture
def client() -> EBSRestClient:
    return EBSRestClient(base_url="http://ebs.example.com:8000/webservices/rest", user="SYSADMIN", password="manager1")


def _mock_response(status_code: int = 200, json_body: dict | None = None) -> Mock:
    resp = Mock()
    resp.status_code = status_code
    resp.text = ""
    resp.json.return_value = json_body or {}
    return resp


class TestCallService:
    """Exercise the shared call_service payload/response handling."""

    def test_success_returns_output_parameters(self, client: EBSRestClient) -> None:
        payload = {
            "OutputParameters": {
                "X_RETURN_STATUS": "S",
                "X_PARTY_ID": "478201",
                "X_PARTY_NUMBER": "59805",
            }
        }
        with patch.object(client.session, "post", return_value=_mock_response(200, payload)) as mock_post:
            result = client.call_service("/hzparty/create_organization/", {"foo": "bar"})

        assert result == payload
        assert mock_post.called
        # Sent URL is base + endpoint
        call_url = mock_post.call_args.args[0]
        assert call_url == "http://ebs.example.com:8000/webservices/rest/hzparty/create_organization/"

    def test_uses_data_jsondumps_not_json_kwarg(self, client: EBSRestClient) -> None:
        """Oracle EBS ISG rejects `application/json; charset=utf-8`; sending via
        `data=json.dumps(...)` keeps Content-Type plain `application/json`."""
        success = {"OutputParameters": {"X_RETURN_STATUS": "S"}}
        with patch.object(client.session, "post", return_value=_mock_response(200, success)) as mock_post:
            client.call_service("/endpoint/", {"key": "value"})

        kwargs = mock_post.call_args.kwargs
        assert "json" not in kwargs
        assert "data" in kwargs
        assert '"key": "value"' in kwargs["data"]

    def test_expected_error_raises_user_exception(self, client: EBSRestClient) -> None:
        payload = {
            "OutputParameters": {
                "X_RETURN_STATUS": "E",
                "X_MSG_COUNT": "1",
                "X_MSG_DATA": "Invalid party number",
            }
        }
        with patch.object(client.session, "post", return_value=_mock_response(200, payload)):
            with pytest.raises(UserException, match="Invalid party number"):
                client.call_service("/endpoint/", {})

    def test_unexpected_error_raises_user_exception(self, client: EBSRestClient) -> None:
        payload = {
            "OutputParameters": {
                "X_RETURN_STATUS": "U",
                "X_MSG_COUNT": "1",
                "X_MSG_DATA": "Unexpected database error",
            }
        }
        with patch.object(client.session, "post", return_value=_mock_response(200, payload)):
            with pytest.raises(UserException, match="Unexpected database error"):
                client.call_service("/endpoint/", {})

    def test_error_with_null_msg_data_falls_back_to_count(self, client: EBSRestClient) -> None:
        """Oracle EBS returns null X_MSG_DATA when multiple errors — we include count."""
        payload = {
            "OutputParameters": {
                "X_RETURN_STATUS": "E",
                "X_MSG_COUNT": "7",
                "X_MSG_DATA": None,
            }
        }
        with patch.object(client.session, "post", return_value=_mock_response(200, payload)):
            with pytest.raises(UserException, match="7 error message"):
                client.call_service("/endpoint/", {})

    def test_http_error_raises_user_exception(self, client: EBSRestClient) -> None:
        resp = _mock_response(500, {})
        resp.text = "Internal Server Error"
        with patch.object(client.session, "post", return_value=resp):
            with pytest.raises(UserException, match="HTTP 500"):
                client.call_service("/endpoint/", {})

    def test_isg_service_fault_raises_user_exception(self, client: EBSRestClient) -> None:
        payload = {"ISGServiceFault": {"faultCode": "SOAP-ENV:Server", "faultMessage": "bad auth"}}
        with patch.object(client.session, "post", return_value=_mock_response(200, payload)):
            with pytest.raises(UserException, match="ISG service fault"):
                client.call_service("/endpoint/", {})


class TestCreateOrganization:
    def test_payload_and_key_order(self, client: EBSRestClient) -> None:
        """Oracle ISG is sensitive to JSON key order — user fields BEFORE CREATED_BY_MODULE."""
        success = {"OutputParameters": {"X_RETURN_STATUS": "S", "X_PARTY_ID": "478201"}}
        with patch.object(client.session, "post", return_value=_mock_response(200, success)) as mock_post:
            result = client.create_organization({"ORGANIZATION_NAME": "Keboola Corp", "DUNS_NUMBER_C": "123"})

        sent = mock_post.call_args.kwargs["data"]
        # ORGANIZATION_NAME must appear before CREATED_BY_MODULE in the serialized payload
        assert sent.index("ORGANIZATION_NAME") < sent.index("CREATED_BY_MODULE")
        assert "TCA_V2_API" in sent
        assert result["OutputParameters"]["X_PARTY_ID"] == "478201"

    def test_endpoint_is_relative(self, client: EBSRestClient) -> None:
        success = {"OutputParameters": {"X_RETURN_STATUS": "S"}}
        with patch.object(client.session, "post", return_value=_mock_response(200, success)) as mock_post:
            client.create_organization({"ORGANIZATION_NAME": "X"})

        url = mock_post.call_args.args[0]
        # Should NOT double /webservices/rest
        assert url.count("/webservices/rest") == 1
        assert url.endswith("/hzparty/create_organization/")


class TestCreatePerson:
    def test_builds_person_payload(self, client: EBSRestClient) -> None:
        success = {"OutputParameters": {"X_RETURN_STATUS": "S", "X_PARTY_ID": "478202"}}
        with patch.object(client.session, "post", return_value=_mock_response(200, success)) as mock_post:
            client.create_person({"PERSON_FIRST_NAME": "Jane", "PERSON_LAST_NAME": "Doe"})

        sent = mock_post.call_args.kwargs["data"]
        assert "P_PERSON_REC" in sent
        assert "Jane" in sent
        # Key order: user fields before CREATED_BY_MODULE
        assert sent.index("PERSON_FIRST_NAME") < sent.index("CREATED_BY_MODULE")


class TestCreateCustAccount:
    def test_splits_account_and_person_records(self, client: EBSRestClient) -> None:
        success = {"OutputParameters": {"X_RETURN_STATUS": "S", "X_CUST_ACCOUNT_ID": "12345"}}
        with patch.object(client.session, "post", return_value=_mock_response(200, success)) as mock_post:
            client.create_cust_account(
                {"ACCOUNT_NAME": "Test Account"},
                {"PERSON_FIRST_NAME": "Jane", "PERSON_LAST_NAME": "Doe"},
            )

        sent = mock_post.call_args.kwargs["data"]
        assert "Test Account" in sent
        assert "Jane" in sent
        assert "P_CREATE_PROFILE_AMT" in sent


class TestCreateArInvoice:
    def test_builds_invoice_with_required_empty_collections(self, client: EBSRestClient) -> None:
        """ORA-06531: empty collections must be [] not null."""
        success = {"OutputParameters": {"X_RETURN_STATUS": "S"}}
        header = {
            "BILL_TO_CUSTOMER_ID": 1000,
            "CUST_TRX_TYPE_ID": 1,
            "TRX_DATE": "2026-04-22T00:00:00",
            "TRX_CURRENCY": "USD",
        }
        lines = [{"QUANTITY_INVOICED": 1, "UNIT_SELLING_PRICE": 500, "DESCRIPTION": "Test"}]

        with patch.object(client.session, "post", return_value=_mock_response(200, success)) as mock_post:
            client.create_ar_invoice(header, lines, org_id=204)

        sent = mock_post.call_args.kwargs["data"]
        assert '"Org_Id": "204"' in sent
        assert '"P_TRX_DIST_TBL_ITEM": []' in sent
        assert '"P_TRX_SALESCREDITS_TBL_ITEM": []' in sent


class TestTestConnection:
    def test_ok_returns_status(self, client: EBSRestClient) -> None:
        resp = Mock()
        resp.status_code = 200
        with patch.object(client.session, "get", return_value=resp):
            result = client.test_connection()
        assert result["status"] == "ok"
        assert result["http_status"] == "200"

    def test_connection_error_raises(self, client: EBSRestClient) -> None:
        import requests

        with patch.object(client.session, "get", side_effect=requests.ConnectionError("refused")):
            with pytest.raises(UserException, match="Cannot reach EBS REST API"):
                client.test_connection()
