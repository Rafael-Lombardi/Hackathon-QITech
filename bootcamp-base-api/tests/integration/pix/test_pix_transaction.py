from os import environ

from tests.utils.requisition import ClientRequisition


INTERNAL_TOKEN = environ.get("INTERNAL_TOKEN", "default_token")


def payload(**overrides):
    value = {
        "id": "pix-integration-001",
        "amount": 150000,
        "transaction_date": "2026-10-17T14:15:00-03:00",
        "source_account": {
            "branch": "0001",
            "account_number": "88213",
            "opening_date": "2025-03-02T10:00:00-03:00",
            "owner": {"document_number": "123.456.789-09"},
        },
        "destination_account": {
            "branch": "3675",
            "account_number": "10442",
            "opening_date": "2025-03-02T09:00:00-03:00",
            "owner": {"document_number": "987.654.321-00"},
        },
        "source": {
            "device_id": "dvc-integration",
            "channel": "app",
            "ip": "198.51.100.23",
            "geolocation": {"latitude": -23.56, "longitude": -46.65},
        },
    }
    value.update(overrides)
    return value


def send(value, token=INTERNAL_TOKEN):
    return ClientRequisition.send(
        "POST",
        "/pix/transaction",
        payload=value,
        headers={"INTERNAL-TOKEN": token},
    )


class TestPixTransaction:
    def test_returns_not_implemented_until_rules_exist(self):
        response = send(payload())
        assert response.response_status == 501

    def test_rejects_invalid_payload(self):
        response = send({"id": "missing-fields"})
        assert response.response_status == 400
        assert response.response_json["code"] == "QIT000001"

    def test_requires_internal_token(self):
        response = send(payload(), token="wrong-token")
        assert response.response_status == 403
        assert response.response_json["code"] == "QIT000002"
