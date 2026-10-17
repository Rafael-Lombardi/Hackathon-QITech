import requests

from connectors.rest_connector import RestConnector
from constants import DICT_API_INTERNAL_TOKEN, DICT_API_TIMEOUT, DICT_API_URL
from errors import DictUnavailable
from utils.document_number import mask_document


class DictConnector(RestConnector):
    """Fala com o DICT do Banco Central — aqui, com o mock dele.

    É a parte de I/O da camada 3 do motor: pergunta quão confiável é o
    titular de um documento e recebe um número de 0 a 100, o trust
    factor (maior é mais confiável). O DICT de verdade não tem essa
    pergunta pronta; o mock do docker-compose.yml faz de conta que tem,
    e a ficção está contada no mockserver/README.md. Só o recebedor é
    consultado, e quem chama é o controller, com o documento do destino.

    Repare no que este connector NÃO decide: o que fazer quando o DICT
    falha. Prazo estourado, ninguém atendendo, 404, 500, corpo fora do
    combinado — tudo vira DictUnavailable, e mais nada. Quem decide que
    isso significa "camada 3 indisponível, o Pix segue sem ela" é o
    controller. Aqui se traduz; lá se decide.

    Até o 404 entra na mesma conta. "O DICT não conhece este documento"
    é falta de informação, e no motor falta de informação nunca vira
    ponto: vira "unavailable", igual ao DICT fora do ar.
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
        except requests.Timeout:
            raise self._unavailable(masked_document, f"no response within {self.timeout} s") from None
        except (requests.RequestException, ValueError) as error:
            # O ValueError é configuração que o requests recusa antes de
            # sair, como um DICT_API_TIMEOUT=0. Pro motor dá no mesmo que
            # a URL errada: o DICT ficou fora de alcance, e o Pix segue.
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
