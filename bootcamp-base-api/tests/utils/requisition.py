import json
from os import environ

from requests import request, Response
from requests.exceptions import ConnectionError as RequestsConnectionError


API_OFFLINE = (
    "Não consegui falar com a API em {base_url}.\n"
    "Ela precisa estar de pé pros testes rodarem. Suba com:  docker compose up -d --build --wait"
)

# Prazo de cada requisição do teste. Sem ele, uma API travada — uma trava
# de banco que nunca solta, por exemplo — penduraria a suíte inteira em
# silêncio; com ele, o teste cai com nome. 30 s é folga de sobra: o Pix
# mais lento da suíte espera os 2 s do DICT lento.
REQUEST_TIMEOUT = 30


def api_base_url() -> str:
    api_host = environ.get("SERVER_LOCALHOST", "0.0.0.0")
    api_port = environ.get("API_PORT", "3000")
    return f"http://{api_host}:{api_port}"


class ClientRequisition:
    @staticmethod
    def send(
        method,
        endpoint,
        payload=None,
        headers=None,
        data=None,
        cert=None,
        query_params=None,
        verify=True,
        timeout=REQUEST_TIMEOUT,
    ):

        if headers is None:
            headers = dict()

        base_url = api_base_url()
        url = f"{base_url}{endpoint}"

        try:
            response = request(
                method.upper(),
                url,
                headers=headers,
                json=payload,
                data=data,
                cert=cert,
                verify=verify,
                params=query_params,
                timeout=timeout,
            )
        except RequestsConnectionError:
            raise RuntimeError(API_OFFLINE.format(base_url=base_url)) from None

        base_response = BaseConnectorResponse(
            endpoint=endpoint,
            method=method,
            payload=payload,
            headers=headers,
            response=response,
        )

        return base_response


class BaseConnectorResponse:
    def __init__(
        self,
        response: Response,
        endpoint: str,
        method: str,
        headers: dict,
        payload: dict,
    ) -> None:
        self.endpoint = endpoint
        self.method = method
        self.payload = payload
        self.headers = headers
        self.response = response
        self.response_content = response.content
        self.response_status = response.status_code

        # O 204 do health check não tem corpo, e um proxy no meio do
        # caminho pode responder HTML. Corpo que não é JSON vira None, e
        # quem esperava JSON descobre isso no assert, com o status na mão.
        self.response_json = None
        if self.response_content:
            try:
                self.response_json = json.loads(self.response_content)
            except ValueError:
                pass
