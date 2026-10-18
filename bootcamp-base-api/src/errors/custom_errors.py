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
    do DICT, vira "unavailable" inteira e a decisão sai assim mesmo. O 503
    só aparece se alguém esquecer o except, e aí ao menos diz a verdade:
    quem caiu foi o DICT.

    reason vai parar no log. Se precisar citar o documento consultado,
    passe pelo mask_document antes.
    """

    code = "QIT001003"

    def __init__(self, reason) -> None:
        title = "DICT Unavailable"
        http_status = 503
        description = f"DICT security information could not be obtained: {reason}."
        translation = "Não foi possível consultar a informação de segurança do DICT."
        super().__init__(title, self.code, http_status, description, translation)


class RestrictionSourceUnavailable(QIException):
    """A fonte de restrições não respondeu, ou respondeu algo que não dá pra usar.

    É a única fonte obrigatória, e o único 503 que chega ao cliente de
    propósito: restrição ativa rejeita por cima de qualquer nota, e sem a
    lista não há como saber se existe uma — presumir lista vazia seria
    deixar passar justamente quem está bloqueado (RFC §7). O Pix sai sem
    veredito e não fica gravado; quem chamou tenta de novo.

    reason vai parar no log. Documento, só pelo mask_document.
    """

    code = "QIT001005"

    def __init__(self, reason) -> None:
        title = "Restriction Source Unavailable"
        http_status = 503
        description = f"Restrictions could not be obtained, so the Pix was not evaluated: {reason}."
        translation = "Não foi possível consultar as restrições; o Pix não foi avaliado."
        super().__init__(title, self.code, http_status, description, translation)
