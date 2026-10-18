"""O roteiro do mock das três fontes: o que cada uma responde, e o que recebeu.

As fontes de fora — o DICT, o cadastro de aparelhos do banco e as
restrições — são, na stack de teste, um MockServer só (o serviço
dict-mock do docker-compose.yml). O mock responde o que estiver
combinado: pra qualquer documento, estatística 70 sem marcação no DICT,
pagador fora do cadastro (404) e lista de restrições vazia. Cada teste
escreve por cima o roteiro das pessoas DELE: o pagador coberto pelo
cadastro com o aparelho de sempre registrado (é o que o new_payer de
identities.py faz, porque a RFC manda semear o cadastro por fixture e
nunca por POST), o recebedor com estatística baixa, a restrição ativa, a
fonte fora do ar. Depois, quando o assunto é quem foi consultado,
pergunta ao mock.

O teste não cria o SourcesMock: pede a fixture sources_mock, de
tests/conftest.py, que apaga no fim tudo o que o teste combinou. O
new_payer cria o dele por conta própria, e o que ele combina fica até
vencer a validade: é cadastro, e precisa durar o teste inteiro.

As armadilhas do mock estão em mockserver/README.md. Este roteiro segue
todas: id determinístico por rota e identidade, prioridade acima da
padrão, validade de 5 minutos, apagar pelo id, e nunca reset nem clear
por path.
"""

import json
from datetime import datetime
from os import environ
from urllib.parse import quote
from uuid import uuid4

import pytest
import requests

# As prioridades, da mais geral pra mais específica: a padrão do arquivo é
# 0; a cobertura de um pagador (qualquer aparelho dele) fica em 5; o
# aparelho registrado, a restrição e a resposta do DICT em 10; e a fonte
# fora do ar pra um pagador em 15, por cima de tudo que ele tem.
COVERAGE_PRIORITY = 5
PRIORITY = 10
OUTAGE_PRIORITY = 15

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


def sources_timeout() -> float:
    """Quanto a API espera cada fonte antes de desistir (SOURCES_TIMEOUT, padrão 2 s)."""
    return float(environ.get("SOURCES_TIMEOUT", "2"))


def security_statistics_path(document: str) -> str:
    return f"/security-statistics/{_only_digits(document)}"


def device_registration_path(document: str, device_id: str | None = None) -> str:
    """O caminho do cadastro: de um aparelho, ou de qualquer aparelho do pagador (regex)."""
    device = ".*" if device_id is None else quote(device_id, safe="")
    return f"/device-registration/{_only_digits(document)}/{device}"


def restrictions_query(**identities: str) -> dict:
    """O pedido de restrições que casa com estas identidades (payer, recipient_holder, recipient_account)."""
    request = {"method": "GET", "path": "/restrictions"}
    if identities:
        request["queryStringParameters"] = {name: [value] for name, value in identities.items()}
    return request


def _only_digits(document: str) -> str:
    return "".join(character for character in document if character.isdigit())


def _iso(moment: datetime | str | None) -> str | None:
    if moment is None or isinstance(moment, str):
        return moment
    return moment.isoformat(timespec="seconds")


class SourcesMock:
    """O roteiro do mock para UM teste (ou para um pagador do new_payer).

    Cria expectativas com id determinístico e, no clear, apaga as que
    criou — e só elas.
    """

    def __init__(self) -> None:
        self._created: list[str] = []

    # ── o cadastro de aparelhos ─────────────────────────────────────

    def cover_payer(self, document: str) -> None:
        """O pagador entra na fixture do cadastro: qualquer aparelho dele responde unregistered.

        Sem isto o cadastro responde 404 (pagador fora da fixture), e o
        P03 sai unavailable, não unregistered.
        """
        self._expect(
            f"device-registration-{_only_digits(document)}",
            COVERAGE_PRIORITY,
            {"method": "GET", "path": device_registration_path(document)},
            {"statusCode": 200, "headers": _JSON, "body": {"registration_status": "unregistered", "provenance": "demo_seed"}},
        )

    def register_device(
        self, document: str, device_id: str, registered_at: datetime | str, revoked_at: datetime | str | None = None,
    ) -> None:
        """O aparelho fica cadastrado pro pagador desde registered_at, até revoked_at se houver."""
        self._expect(
            f"device-registration-{_only_digits(document)}-{device_id}",
            PRIORITY,
            {"method": "GET", "path": device_registration_path(document, device_id)},
            {
                "statusCode": 200,
                "headers": _JSON,
                "body": {
                    "registration_status": "registered",
                    "registered_at": _iso(registered_at),
                    "revoked_at": _iso(revoked_at),
                    "provenance": "demo_seed",
                },
            },
        )

    def registry_down(self, document: str) -> None:
        """O cadastro responde 503 pra qualquer aparelho deste pagador: fonte fora do ar."""
        self._expect(
            f"device-registration-{_only_digits(document)}-down",
            OUTAGE_PRIORITY,
            {"method": "GET", "path": device_registration_path(document)},
            {"statusCode": 503},
        )

    # ── as restrições ───────────────────────────────────────────────

    def restrict(
        self,
        scope: str,
        identity_key: str,
        *,
        effective_at: datetime | str = "2026-01-01T00:00:00-03:00",
        expires_at: datetime | str | None = None,
        revoked_at: datetime | str | None = None,
        reason: str = "restrição simulada",
        restriction_id: str | None = None,
    ) -> dict:
        """Uma restrição com este scope (payer, recipient_holder, recipient_account) e esta identidade.

        identity_key é o documento, ou ispb:agência:conta no scope da
        conta exata. Devolve a restrição como o mock a entrega, pra que o
        teste compare com o que a API devolve em metadata.restrictions.
        """
        restriction = {
            "restriction_id": restriction_id or f"rst-{uuid4().hex[:12]}",
            "scope": scope,
            "identity_key": identity_key,
            "reason": reason,
            "evidence_ref": "demo-ref-1",
            "provenance": "demo_seed",
            "effective_at": _iso(effective_at),
            "expires_at": _iso(expires_at),
            "revoked_at": _iso(revoked_at),
        }
        self._expect(
            f"restrictions-{scope}-{identity_key}",
            PRIORITY,
            restrictions_query(**{scope: identity_key}),
            {"statusCode": 200, "headers": _JSON, "body": {"restrictions": [restriction]}},
        )
        return restriction

    def restrictions_down(self, payer_document: str) -> None:
        """A fonte de restrições responde 503 a qualquer Pix deste pagador."""
        self._expect(
            f"restrictions-down-{_only_digits(payer_document)}",
            OUTAGE_PRIORITY,
            restrictions_query(payer=_only_digits(payer_document)),
            {"statusCode": 503},
        )

    # ── o DICT ──────────────────────────────────────────────────────

    def set_security_statistics(self, document: str, statistics, fraud_marker: str | None = None) -> None:
        """O DICT responde 200 com esta estatística para o documento — e com a marcação, se houver.

        fraud_marker é "suspicious", "confirmed" ou None (sem marcação, que
        o DICT escreve como null).
        """
        self._expect_dict(document, {
            "statusCode": 200,
            "headers": _JSON,
            "body": {"security_statistics": statistics, "fraud_marker": fraud_marker},
        })

    def set_delay(self, document: str, seconds: int, statistics: int = 0) -> None:
        """A resposta vem, mas só depois de `seconds` segundos.

        Repare na confiança 0 da resposta atrasada: se a API esperasse por
        ela, o DICT01 dispararia. Ver o sinal unavailable prova que a API
        desistiu no prazo, e não que leu outra coisa.
        """
        self._expect_dict(document, {
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
        self._expect_dict(document, {"statusCode": status_code, "headers": _JSON, "body": {"security_statistics": 0}})

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
        self._expect_dict(document, {"statusCode": 200, "headers": _JSON, "body": payload})

    def verify_called(self, document: str, times: int = 1) -> None:
        """A API consultou o DICT sobre este documento exatamente `times` vezes."""
        self._verify(document, {"atLeast": times, "atMost": times}, f"{times} vez(es)")

    def verify_not_called(self, document: str) -> None:
        """A API nunca consultou o DICT sobre este documento."""
        self._verify(document, {"atMost": 0}, "nenhuma vez")

    # ── a limpeza ───────────────────────────────────────────────────

    def clear(self) -> None:
        """Apaga, pelo id, cada expectativa que este roteiro criou."""
        url = f"{mock_base_url()}/mockserver/clear"
        for expectation_id in self._created:
            response = requests.put(url, params={"type": "EXPECTATIONS"}, json={"id": expectation_id}, timeout=_TIMEOUT)
            if not response.ok:
                pytest.fail(f"O mock não apagou {expectation_id}: HTTP {response.status_code} {response.text}")
        self._created.clear()

    def _expect_dict(self, document: str, http_response: dict) -> None:
        digits = _only_digits(document)
        self._expect(
            f"security-statistics-{digits}",
            PRIORITY,
            {"method": "GET", "path": security_statistics_path(digits)},
            http_response,
        )

    def _expect(self, expectation_id: str, priority: int, http_request: dict, http_response: dict) -> None:
        expectation = {
            "id": expectation_id,
            "priority": priority,
            "httpRequest": http_request,
            "httpResponse": http_response,
            "timeToLive": TIME_TO_LIVE,
        }
        response = requests.put(f"{mock_base_url()}/mockserver/expectation", json=expectation, timeout=_TIMEOUT)
        if not response.ok:
            pytest.fail(f"O mock recusou a expectativa {expectation_id}: HTTP {response.status_code} {response.text}")
        if expectation_id not in self._created:
            self._created.append(expectation_id)

    def _verify(self, document: str, times: dict, expected: str) -> None:
        request = {"httpRequest": {"method": "GET", "path": security_statistics_path(document)}, "times": times}
        response = requests.put(f"{mock_base_url()}/mockserver/verify", json=request, timeout=_TIMEOUT)
        if response.status_code != 202:
            pytest.fail(
                f"Esperava a API consultando GET {security_statistics_path(document)} {expected}, "
                f"e o mock diz outra coisa (HTTP {response.status_code}):\n{response.text}"
            )
