"""O Pix pela porta da frente: monta o payload, manda, lê a análise.

pix_payload monta um Pix válido no contrato do desafio, com a data
sempre explícita; post_pix e get_pix chamam as duas rotas já com o
INTERNAL-TOKEN, e post_raw manda bytes crus, pros testes do corpo que
nem chega a ser JSON; seed e seed_pix mandam o histórico do cenário, Pix
a Pix, conferindo que cada semente terminou como o cenário precisa.
"""

from datetime import datetime
from urllib.parse import quote
from uuid import uuid4

import pytest

from tests.utils import INTERNAL_TOKEN
from tests.utils.clock import T0, iso
from tests.utils.identities import Payer, Recipient
from tests.utils.requisition import ClientRequisition

# R$ 50. Valor pequeno por padrão: longe do L01 (R$ 1.000 à noite), do
# L02 (R$ 200 em aparelho novo) e do P06 (só a partir de R$ 1.000).
SMALL_AMOUNT = 5000

GEOLOCATION = {"latitude": -23.56, "longitude": -46.65}


def pix_payload(
    payer: Payer,
    recipient: Recipient,
    when: datetime | str = T0,
    amount: int = SMALL_AMOUNT,
    *,
    device_id: str | None = None,
    ip: str | None = None,
    geolocation: dict | None = GEOLOCATION,
    payer_document: str | None = None,
    recipient_document: str | None = None,
    channel: str = "app",
    pix_id: str | None = None,
) -> dict:
    """Um Pix válido de `payer` para `recipient`, no instante `when`.

    A data é SEMPRE explícita: quem decide quando o Pix aconteceu é o
    teste. `when` aceita texto pronto para o caso em que o formato é o
    assunto (payload em UTC, com "Z"). Aparelho e IP vêm do pagador, e
    trocar um deles é como se monta o P03 e o P09. Documento com
    pontuação entra por payer_document / recipient_document.
    """
    source = {
        "device_id": device_id or payer.device_id,
        "channel": channel,
        "ip": ip or payer.ip,
    }
    if geolocation is not None:
        source["geolocation"] = dict(geolocation)

    return {
        "id": pix_id or f"pix-{uuid4()}",
        "amount": amount,
        "transaction_date": when if isinstance(when, str) else iso(when),
        "source_account": {
            "branch": payer.account.branch,
            "account_number": payer.account.account_number,
            "opening_date": iso(payer.account.opening_date),
            "owner": {"document_number": payer_document or payer.document},
        },
        "destination_account": {
            "participant": recipient.account.participant,
            "branch": recipient.account.branch,
            "account_number": recipient.account.account_number,
            "opening_date": iso(recipient.account.opening_date),
            "owner": {"document_number": recipient_document or recipient.document},
        },
        "source": source,
    }


class PixResponse:
    """A resposta de uma das rotas Pix: o status HTTP, o corpo lido e os bytes.

    Os atalhos (status, decision_rule, scores, signal...) leem a análise
    de dentro do corpo. Chamados numa resposta que não é análise — um 400,
    um 500 —, falham dizendo o que veio, em vez de um KeyError mudo.
    `content` são os bytes como chegaram, pra quem compara corpos de
    verdade, e não o JSON já lido.
    """

    def __init__(self, http_status: int, body, content: bytes = b"") -> None:
        self.http_status = http_status
        self.body = body
        self.content = content

    @property
    def status(self) -> str:
        return self._analysis()["status"]

    @property
    def metadata(self) -> dict:
        return self._analysis()["metadata"]

    @property
    def decision_rule(self) -> str:
        return self.metadata["decision_rule"]

    @property
    def reasons(self) -> list:
        return self.metadata["reasons"]

    @property
    def scores(self) -> dict:
        return self.metadata["scores"]

    @property
    def sufficiency(self) -> dict:
        return self.metadata["evidence_sufficiency"]

    def signal(self, code: str) -> dict:
        found = [signal for signal in self.metadata["signals"] if signal.get("code") == code]
        if len(found) != 1:
            codes = [signal.get("code") for signal in self.metadata["signals"]]
            raise AssertionError(f"Esperava o sinal {code} uma vez em metadata.signals, e veio: {codes}")
        return found[0]

    def signal_status(self, code: str) -> str:
        return self.signal(code)["status"]

    def signals_with(self, status: str) -> set[str]:
        return {signal["code"] for signal in self.metadata["signals"] if signal["status"] == status}

    def triggered(self) -> set[str]:
        return self.signals_with("triggered")

    def _analysis(self) -> dict:
        if not isinstance(self.body, dict) or "metadata" not in self.body:
            raise AssertionError(f"A resposta não é uma análise: HTTP {self.http_status}, corpo {self.body!r}")
        return self.body

    def __repr__(self) -> str:
        return f"PixResponse(http_status={self.http_status}, body={self.body!r})"


def post_pix(payload: dict, token: str | None = INTERNAL_TOKEN) -> PixResponse:
    """POST /pix/transaction. token=None manda sem o header."""
    response = ClientRequisition.send("POST", "/pix/transaction", payload=payload, headers=_token_header(token))
    return _pix_response(response)


def post_raw(data, content_type: str | None = "application/json") -> PixResponse:
    """POST /pix/transaction com o corpo em bytes crus, do jeito que vier.

    `data` pode ser um gerador: aí o requests manda em pedaços (chunked),
    sem Content-Length. content_type=None manda sem o header.
    """
    headers = _token_header(INTERNAL_TOKEN)
    if content_type is not None:
        headers["Content-Type"] = content_type
    response = ClientRequisition.send("POST", "/pix/transaction", headers=headers, data=data)
    return _pix_response(response)


def get_pix(pix_id: str, token: str | None = INTERNAL_TOKEN) -> PixResponse:
    """GET /pix/transaction/{id}, o `id` do payload."""
    endpoint = f"/pix/transaction/{quote(pix_id, safe='')}"
    response = ClientRequisition.send("GET", endpoint, headers=_token_header(token))
    return _pix_response(response)


def seed(payloads: list[dict], expect: str | list[str] = "approved") -> list[PixResponse]:
    """Manda o histórico do cenário, um Pix por vez, na ordem da lista.

    Semente não é atalho: ela passa pelo motor como qualquer Pix, e o que
    ela deixa no histórico depende de como terminou. IP conhecido, par
    recorrente e mediana só contam Pix aprovados — uma semente que
    voltasse 2FA deixaria o IP sem Pix aprovado, e o teste seguinte
    mediria outra coisa sem avisar. Por isso cada uma é conferida aqui, e
    a primeira que destoa derruba o teste dizendo qual.

    `expect` é um status para todas ou uma lista, um por semente.
    """
    if isinstance(expect, str):
        expect = [expect] * len(payloads)
    assert len(expect) == len(payloads), "seed: um status esperado por semente"

    responses = []
    for position, (payload, expected) in enumerate(zip(payloads, expect), start=1):
        response = post_pix(payload)
        status = response.body.get("status") if isinstance(response.body, dict) else None
        if response.http_status != 201 or status != expected:
            pytest.fail(_broken_seed(position, len(payloads), payload, response, expected))
        responses.append(response)
    return responses


def seed_pix(
    payer: Payer,
    recipient: Recipient,
    moments: list[datetime],
    expect: str | list[str] = "approved",
    **payload_options,
) -> list[PixResponse]:
    """Um Pix de `payer` para `recipient` em cada instante de `moments`."""
    payloads = [pix_payload(payer, recipient, moment, **payload_options) for moment in moments]
    return seed(payloads, expect)


def _pix_response(response) -> PixResponse:
    return PixResponse(response.response_status, response.response_json, response.response_content)


def _token_header(token: str | None) -> dict:
    return {} if token is None else {"INTERNAL-TOKEN": token}


def _broken_seed(position: int, total: int, payload: dict, response: PixResponse, expected: str) -> str:
    when = payload.get("transaction_date")
    try:
        got = f"{response.status} pelo {response.decision_rule}, sinais {sorted(response.triggered())}, motivos {response.reasons}"
    except (AssertionError, KeyError):
        got = f"HTTP {response.http_status}, corpo {response.body!r}"
    return (
        f"A semente {position} de {total} ({when}) devia terminar {expected} e voltou: {got}.\n"
        "O histórico montado não é o que o teste descreve, e o resto do cenário mediria outra "
        "coisa. Confira a conta no docstring do teste."
    )
