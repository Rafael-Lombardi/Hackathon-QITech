import requests

from connectors.rest_connector import RestConnector
from constants import DICT_API_INTERNAL_TOKEN, DICT_API_TIMEOUT, DICT_API_URL
from errors import DictUnavailable
from utils.document_number import mask_document


class DictConnector(RestConnector):
    """Fala com o DICT do Banco Central — aqui, com o mock dele.

    DICT é o Diretório de Identificadores de Contas Transacionais, o
    cadastro do Banco Central que liga chave Pix a conta. O de verdade
    devolve contadores de fraude por CPF/CNPJ (Manual do DICT, cap. 18);
    o time aceitou a ficção de um DICT que já responde a nota: o trust
    factor do titular de um documento, de 0 a 100, e maior é mais
    confiável. É a parte de I/O da camada 3 do motor, e quem faz o papel
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

    def get_trust_factor(self, document_number: str) -> int:
        """Devolve o trust factor do titular do documento, de 0 a 100.

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

        trust_factor = None
        if isinstance(response.json, dict):
            trust_factor = response.json.get("trust_factor")

        # Repare no `type(...) is int` no lugar do isinstance: em Python,
        # True é um int que vale 1, e um {"trust_factor": true} passaria
        # como trust factor 1. O 70.0 e o "70" também ficam de fora — o
        # combinado é um inteiro, e resposta fora do combinado não vira
        # número por boa vontade nossa.
        if type(trust_factor) is not int or not 0 <= trust_factor <= 100:
            raise self._unavailable(masked_document, "body without an integer trust_factor from 0 to 100")

        return trust_factor

    def _unavailable(self, masked_document: str, reason: str) -> DictUnavailable:
        # Toda desistência deixa uma linha dizendo por quê. Sem ela, o
        # corpo inválido nem apareceria no log: pro RestConnector, foi
        # um 200 como outro qualquer.
        self.logger.warning(f"DICT UNAVAILABLE {masked_document} - {reason}")
        return DictUnavailable(reason)
