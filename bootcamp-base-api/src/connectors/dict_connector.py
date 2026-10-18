import requests

from connectors.rest_connector import RestConnector
from constants import DICT_API_INTERNAL_TOKEN, DICT_API_TIMEOUT, DICT_API_URL
from errors import DictUnavailable
from utils.document_number import mask_document


class DictConnector(RestConnector):
    """Fala com o DICT do Banco Central — aqui, com o mock dele.

    DICT é o Diretório de Identificadores de Contas Transacionais, o
    cadastro do Banco Central que liga chave Pix a conta. O de verdade
    devolve estatísticas de segurança e marcações de fraude; o mock reduz
    as estatísticas a um indicador numérico para os cenários. É a parte de
    I/O da camada 3 do motor, e quem faz o papel
    do DICT é o mock do docker-compose.yml (mockserver/README.md).

    Repare no que este connector NÃO decide: o que fazer quando o DICT
    falha. Prazo estourado, ninguém atendendo, status ou corpo fora do
    combinado — tudo vira DictUnavailable, e mais nada. O que isso
    significa pro Pix, quem decide é o controller. O combinado é 200 com
    um número pra qualquer documento; o resto, 404 incluído, é fora do
    combinado.
    """

    def __init__(self) -> None:
        super().__init__(
            class_name=__name__,
            base_url=DICT_API_URL,
            timeout=DICT_API_TIMEOUT,
            internal_token=DICT_API_INTERNAL_TOKEN,
        )

    def get_security_statistics(self, document_number: str) -> int:
        """Devolve indicador numérico mockado das estatísticas DICT.

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
                endpoint=f"/trust-factor/{document_number}",
                method="GET",
                log_as=f"/trust-factor/{masked_document}",
            )
        except requests.RequestException as error:
            raise self._unavailable(masked_document, f"request failed ({type(error).__name__})") from None

        if response.status != 200:
            raise self._unavailable(masked_document, f"status {response.status}")

        security_statistics = None
        if isinstance(response.json, dict):
            security_statistics = response.json.get("security_statistics")
            if security_statistics is None:  # formato antigo do mock
                security_statistics = response.json.get("trust_factor")

        # Repare no `type(...) is int` no lugar do isinstance: em Python,
        # True é um int que vale 1, e um {"trust_factor": true} passaria
        # como estatística DICT 1. O 70.0 e o "70" também ficam de fora — o
        # combinado é um inteiro, e resposta fora do combinado não vira
        # número por boa vontade nossa.
        if type(security_statistics) is not int or not 0 <= security_statistics <= 100:
            raise self._unavailable(masked_document, "body without integer DICT security statistics")

        return security_statistics

    def _unavailable(self, masked_document: str, reason: str) -> DictUnavailable:
        # Toda desistência deixa uma linha dizendo por quê. Sem ela, o
        # corpo inválido nem apareceria no log: pro RestConnector, foi
        # um 200 como outro qualquer.
        self.logger.warning(f"DICT UNAVAILABLE {masked_document} - {reason}")
        return DictUnavailable(reason)
