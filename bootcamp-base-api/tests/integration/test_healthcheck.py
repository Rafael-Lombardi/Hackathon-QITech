from tests.utils import INTERNAL_TOKEN
from tests.utils.requisition import ClientRequisition


FORBIDDEN_TRANSLATION = "Requisição precisa ser interna"
NOT_FOUND_TRANSLATION = "O resource solicitado não pode ser encontrado, mas pode estar disponível no futuro. Requests subsequentes do cliente são permitidos."


class TestHealthCheck:
    def test_home(self):
        response = ClientRequisition.send("GET", "/")
        assert response.response_status == 200

    def test_health_check(self):
        response = ClientRequisition.send("GET", "/health_check")
        assert response.response_status == 204

    def test_no_token(self):
        response = ClientRequisition.send("PUT", "/sample")
        assert response.response_status == 403
        assert response.response_json["code"] == "QIT000002"

    def test_sink(self):
        response = ClientRequisition.send(
            "PUT",
            "/sample",
            headers={"INTERNAL-TOKEN": INTERNAL_TOKEN},
        )
        assert response.response_status == 404
        assert response.response_json["code"] == "QIT000404"

    def test_method_not_allowed(self):
        response = ClientRequisition.send("PUT", "/health_check")
        assert response.response_status == 405
        assert response.response_json["code"] == "QIT000405"

    def test_error_translation_keeps_accents(self):
        response = ClientRequisition.send("PUT", "/sample")
        assert response.response_status == 403
        assert response.response_json["code"] == "QIT000002"
        assert response.response_json["translation"] == FORBIDDEN_TRANSLATION

        response = ClientRequisition.send(
            "PUT",
            "/sample",
            headers={"INTERNAL-TOKEN": INTERNAL_TOKEN},
        )
        assert response.response_status == 404
        assert response.response_json["code"] == "QIT000404"
        assert response.response_json["translation"] == NOT_FOUND_TRANSLATION
