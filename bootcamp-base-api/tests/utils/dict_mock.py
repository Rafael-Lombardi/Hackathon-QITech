"""O roteiro do mock do DICT: o que ele responde, por documento, e o que ele recebeu.

O DICT é o diretório do Banco Central; aqui é um MockServer que responde
GET /security-statistics/{documento} com {"security_statistics": 70} para qualquer
documento. Cada teste que trata da camada 3 escreve o roteiro do
recebedor DELE — confiança baixa, resposta lenta, erro — e depois
pergunta ao mock se a API ligou, para quem e quantas vezes.

O teste não cria o DictMock: pede a fixture dict_mock, de
tests/conftest.py, que apaga no fim tudo o que o teste combinou.

As armadilhas do mock estão em mockserver/README.md. Este roteiro segue
todas: id por documento, prioridade 10, validade de 5 minutos, apagar
pelo id, e nunca reset nem clear por path.
"""

import json
from os import environ

import pytest
import requests

from tests.utils.identities import only_digits

PRIORITY = 10

# Folga de sobra pro teste mais lento que usa o mock (uns 2 s), e curta o
# bastante pra que uma rodada interrompida no meio não deixe lixo por
# muito tempo.
TIME_TO_LIVE = {"timeUnit": "MINUTES", "timeToLive": 5}

_JSON = {"Content-Type": ["application/json"]}
_TIMEOUT = 5


def mock_base_url() -> str:
    host = environ.get("SERVER_LOCALHOST", "0.0.0.0")
    port = environ.get("DICT_MOCK_PORT", "1080")
    return f"http://{host}:{port}"


def dict_timeout() -> float:
    """Quanto a API espera o DICT antes de desistir (DICT_API_TIMEOUT, padrão 2 s)."""
    return float(environ.get("DICT_API_TIMEOUT", "2"))


def security_statistics_path(document: str) -> str:
    return f"/security-statistics/{only_digits(document)}"


class DictMock:
    """O roteiro do mock para UM teste.

    Cria expectativas por documento e, no clear, apaga as que criou — e
    só elas.
    """

    def __init__(self) -> None:
        self._created: list[str] = []

    def set_security_statistics(self, document: str, statistics) -> None:
        """O DICT responde 200 com esta estatística para o documento."""
        self._expect(document, {"statusCode": 200, "headers": _JSON, "body": {"security_statistics": statistics}})

    def set_delay(self, document: str, seconds: int, statistics: int = 0) -> None:
        """A resposta vem, mas só depois de `seconds` segundos.

        Repare na confiança 0 da resposta atrasada: se a API esperasse por
        ela, o DICT01 dispararia. Ver o sinal unavailable prova que a API
        desistiu no prazo, e não que leu outra coisa.
        """
        self._expect(document, {
            "statusCode": 200,
            "headers": _JSON,
            "body": {"security_statistics": statistics},
            "delay": {"timeUnit": "SECONDS", "value": seconds},
        })

    def set_status(self, document: str, status_code: int) -> None:
        """O DICT responde com erro, e com confiança 0 no corpo.

        A confiança 0 é de propósito: a API que lesse o corpo sem olhar o
        status dispararia o DICT01.
        """
        self._expect(document, {"statusCode": status_code, "headers": _JSON, "body": {"security_statistics": 0}})

    def set_body(self, document: str, body) -> None:
        """O DICT responde 200 com este corpo, torto ou não. Texto vai como está.

        Aqui o corpo vai na forma explícita do MockServer (type JSON ou
        STRING): um corpo torto qualquer, solto, poderia ser lido pelo mock
        como instrução dele, e não como o que ele deve devolver.
        """
        if isinstance(body, str):
            payload = {"type": "STRING", "string": body}
        else:
            payload = {"type": "JSON", "json": json.dumps(body)}
        self._expect(document, {"statusCode": 200, "headers": _JSON, "body": payload})

    def verify_called(self, document: str, times: int = 1) -> None:
        """A API consultou este documento exatamente `times` vezes."""
        self._verify(document, {"atLeast": times, "atMost": times}, f"{times} vez(es)")

    def verify_not_called(self, document: str) -> None:
        """A API nunca consultou este documento."""
        self._verify(document, {"atMost": 0}, "nenhuma vez")

    def clear(self) -> None:
        """Apaga, pelo id, cada expectativa que este roteiro criou."""
        url = f"{mock_base_url()}/mockserver/clear"
        for expectation_id in self._created:
            response = requests.put(url, params={"type": "EXPECTATIONS"}, json={"id": expectation_id}, timeout=_TIMEOUT)
            if not response.ok:
                pytest.fail(f"O mock do DICT não apagou {expectation_id}: HTTP {response.status_code} {response.text}")
        self._created.clear()

    def _expect(self, document: str, http_response: dict) -> None:
        digits = only_digits(document)
        expectation = {
            "id": f"security-statistics-{digits}",
            "priority": PRIORITY,
            "httpRequest": {"method": "GET", "path": security_statistics_path(digits)},
            "httpResponse": http_response,
            "timeToLive": TIME_TO_LIVE,
        }
        response = requests.put(f"{mock_base_url()}/mockserver/expectation", json=expectation, timeout=_TIMEOUT)
        if not response.ok:
            pytest.fail(f"O mock do DICT recusou a expectativa de {digits}: HTTP {response.status_code} {response.text}")
        if expectation["id"] not in self._created:
            self._created.append(expectation["id"])

    def _verify(self, document: str, times: dict, expected: str) -> None:
        request = {"httpRequest": {"method": "GET", "path": security_statistics_path(document)}, "times": times}
        response = requests.put(f"{mock_base_url()}/mockserver/verify", json=request, timeout=_TIMEOUT)
        if response.status_code != 202:
            pytest.fail(
                f"Esperava a API consultando GET {security_statistics_path(document)} {expected}, "
                f"e o mock diz outra coisa (HTTP {response.status_code}):\n{response.text}"
            )
