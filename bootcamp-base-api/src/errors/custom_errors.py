"""Os erros do antifraude Pix — uma classe por recusa, no formato do Base Service.

Os códigos seguem a numeração do repositório: QIT000xxx são os do Base
Service (schema, token, rota), QIT001xxx são os deste serviço. Repare que o
error_verification(), que roda quando a API sobe e recusa código repetido,
só enxerga a classe exportada em errors/__init__.py.
"""

from errors.base_error import QIException


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

    409 é a família do conflito: o pedido em si está bem formado, e o que
    impede é o que já está gravado. Reenvio IDÊNTICO não cai aqui — volta
    200 com a análise gravada, que é a idempotência. Já um id reaproveitado
    com outro valor ou outro destino é outro Pix dizendo ser o mesmo, e a
    API não escolhe qual dos dois é o verdadeiro.
    """

    code = "QIT001002"

    def __init__(self, external_id) -> None:
        title = "Pix Transaction Id Conflict"
        http_status = 409
        description = f"Pix transaction with id {external_id} was already received with a different payload."
        translation = f"Já existe um Pix com id {external_id} e conteúdo diferente."
        super().__init__(title, self.code, http_status, description, translation)


class DictUnavailable(QIException):
    """O DICT não respondeu, ou respondeu algo que não dá pra usar.

    Erro INTERNO: o DictConnector levanta e o controller captura — a camada
    3 vira unavailable e a decisão sai assim mesmo. O cliente nunca deveria
    ver este erro. O 503 só aparece se alguém esquecer o except, e aí ao
    menos diz a verdade: quem caiu foi o DICT.

    reason vai parar no log. Se precisar citar o documento consultado,
    passe pelo mask_document antes.
    """

    code = "QIT001003"

    def __init__(self, reason) -> None:
        title = "DICT Unavailable"
        http_status = 503
        description = f"The DICT trust factor could not be obtained: {reason}"
        translation = "Não foi possível consultar o fator de confiança no DICT."
        super().__init__(title, self.code, http_status, description, translation)
