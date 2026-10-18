from dataclasses import dataclass

import requests

from antifraud.contract import FraudMarker
from connectors.rest_connector import RestConnector
from constants import DICT_API_URL, SOURCES_INTERNAL_TOKEN, SOURCES_TIMEOUT
from errors import DictUnavailable
from utils.document_number import mask_document

# As marcações que o DICT escreve no corpo, pelo texto. "none" não entra
# de propósito: sem marcação o DICT manda null (ou nem manda a chave), e
# um texto fora desta lista é corpo fora do combinado.
_MARKERS_BY_TEXT = {marker.value: marker for marker in (FraudMarker.SUSPICIOUS, FraudMarker.CONFIRMED)}


@dataclass(frozen=True)
class DictSecurityInformation:
    """O que o DICT diz de um documento: a estatística de segurança e a marcação de fraude."""

    statistics: int  # 0 a 100
    fraud_marker: FraudMarker


class DictConnector(RestConnector):
    """Fala com o DICT do Banco Central — aqui, com o mock dele.

    DICT é o Diretório de Identificadores de Contas Transacionais, o
    cadastro do Banco Central que liga chave Pix a conta. O de verdade
    devolve estatísticas de segurança e marcações de fraude; o mock reduz
    as estatísticas a um número e traz a marcação, quando há, no mesmo
    corpo. É a parte de I/O da camada 3 do motor, e quem faz o papel do
    DICT é o mock do docker-compose.yml (mockserver/README.md).

    Repare no que este connector NÃO decide: o que fazer quando o DICT
    falha. Prazo estourado, ninguém atendendo, status ou corpo fora do
    combinado — tudo vira DictUnavailable, e mais nada. O que isso
    significa pro Pix, quem decide é o controller. O combinado é 200 com
    a estatística (inteiro de 0 a 100) e, opcional, a marcação
    (suspicious, confirmed ou null) pra qualquer documento; o resto, 404
    incluído, é fora do combinado.
    """

    def __init__(self) -> None:
        super().__init__(
            class_name=__name__,
            base_url=DICT_API_URL,
            timeout=SOURCES_TIMEOUT,
            internal_token=SOURCES_INTERNAL_TOKEN,
        )

    def get_security_information(self, document_number: str) -> DictSecurityInformation:
        """A estatística de segurança e a marcação de fraude de um documento.

        O documento chega só com dígitos: quem normaliza é o
        utils/document_number.py, antes daqui. Qualquer falha levanta
        DictUnavailable.
        """
        masked_document = mask_document(document_number)

        # O `from None` deixa o erro do requests aqui dentro. Lá fora ele
        # não faz falta — o log já contou o que houve — e a mensagem dele
        # repete o endereço inteiro, documento junto.
        try:
            response = self.send(
                endpoint=f"/security-statistics/{document_number}",
                method="GET",
                log_as=f"/security-statistics/{masked_document}",
            )
        except requests.RequestException as error:
            raise self._unavailable(masked_document, f"request failed ({type(error).__name__})") from None

        if response.status != 200:
            raise self._unavailable(masked_document, f"status {response.status}")

        body = response.json if isinstance(response.json, dict) else {}
        security_statistics = body.get("security_statistics")

        # Repare no `type(...) is int` no lugar do isinstance: em Python,
        # True é um int que vale 1, e um {"security_statistics": true} passaria
        # como estatística DICT 1. O 70.0 e o "70" também ficam de fora — o
        # combinado é um inteiro, e resposta fora do combinado não vira
        # número por boa vontade nossa.
        if type(security_statistics) is not int or not 0 <= security_statistics <= 100:
            raise self._unavailable(masked_document, "body without integer DICT security statistics")

        # Sem marcação, o DICT manda null ou nem manda a chave. Qualquer
        # outro valor que não seja uma das marcações é corpo fora do
        # combinado, e vale pra resposta inteira: a estatística não é
        # aproveitada de uma resposta que não entendemos.
        raw_marker = body.get("fraud_marker")
        if raw_marker is None:
            fraud_marker = FraudMarker.NONE
        elif isinstance(raw_marker, str) and raw_marker in _MARKERS_BY_TEXT:
            fraud_marker = _MARKERS_BY_TEXT[raw_marker]
        else:
            raise self._unavailable(masked_document, "body with unknown DICT fraud marker")

        return DictSecurityInformation(statistics=security_statistics, fraud_marker=fraud_marker)

    def _unavailable(self, masked_document: str, reason: str) -> DictUnavailable:
        # Toda desistência deixa uma linha dizendo por quê. Sem ela, o
        # corpo inválido nem apareceria no log: pro RestConnector, foi
        # um 200 como outro qualquer.
        self.logger.warning(f"DICT UNAVAILABLE {masked_document} - {reason}")
        return DictUnavailable(reason)
