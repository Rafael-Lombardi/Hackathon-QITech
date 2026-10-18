"""Os erros do antifraude Pix — uma classe por erro, no formato do Base Service.

Os códigos seguem a numeração do repositório: QIT000xxx são os do Base
Service (schema, token, rota), QIT001xxx são os deste serviço. Repare que o
error_verification(), que roda quando a API sobe e recusa código repetido,
só enxerga a classe exportada em errors/__init__.py.
"""

from errors import QIException


class PixTransactionNotFound(QIException):
    """GET de um id que nunca chegou por aqui."""

    code = "QIT001001"

    def __init__(self, external_id) -> None:
        title = "Pix Transaction not Found"
        http_status = 404
        description = f"Pix transaction with id {external_id} was not found."
        translation = f"O Pix com id {external_id} não foi encontrado."
        super().__init__(title, self.code, http_status, description, translation)


class PixTransactionIdConflict(QIException):
    """O mesmo id chegou de novo com outro conteúdo.

    409 Conflict: o pedido está bem formado, e o que impede é o que já está
    gravado. Reenvio IDÊNTICO não cai aqui — volta 200 com a análise
    gravada, que é a idempotência. Já um id reaproveitado com outro valor ou
    outro destino é outro Pix dizendo ser o mesmo, e a API não escolhe qual
    dos dois é o verdadeiro.
    """

    code = "QIT001002"

    def __init__(self, external_id) -> None:
        title = "Pix Transaction Id Conflict"
        http_status = 409
        description = f"Pix transaction with id {external_id} was already received with a different payload."
        translation = f"O Pix com id {external_id} já foi recebido com outro conteúdo."
        super().__init__(title, self.code, http_status, description, translation)


class DictUnavailable(QIException):
    """O DICT não respondeu, ou respondeu algo que não dá pra usar.

    O DICT é o diretório de chaves Pix do Banco Central. Erro INTERNO: o
    DictConnector levanta e o controller captura — a camada 3 do motor, a
    do DICT, vira "unavailable" e a decisão sai assim mesmo. O 503 só
    aparece se alguém esquecer o except, e aí ao menos diz a verdade: quem
    caiu foi o DICT.

    reason vai parar no log. Se precisar citar o documento consultado,
    passe pelo mask_document antes.
    """

    code = "QIT001003"

    def __init__(self, reason) -> None:
        title = "DICT Unavailable"
        http_status = 503
        description = f"DICT security evidence could not be obtained: {reason}."
        translation = "Não foi possível consultar a evidência de segurança do DICT."
        super().__init__(title, self.code, http_status, description, translation)


class PixTransactionProcessing(QIException):
    code = "QIT001004"

    def __init__(self, external_id) -> None:
        super().__init__(
            "Pix Transaction Processing",
            self.code,
            409,
            f"Pix transaction with id {external_id} is still being evaluated.",
            f"O Pix com id {external_id} ainda está sendo avaliado.",
            headers={"Retry-After": "2"},
        )


class PixTransactionEvaluationFailed(QIException):
    code = "QIT001005"

    def __init__(self, external_id) -> None:
        super().__init__(
            "Pix Transaction Evaluation Failed",
            self.code,
            503,
            f"Pix transaction with id {external_id} could not be evaluated.",
            f"O Pix com id {external_id} não pôde ser avaliado.",
        )
