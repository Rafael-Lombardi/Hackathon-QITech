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


class TransactionDateOutOfTolerance(QIException):
    """A data do Pix está longe demais do relógio do servidor.

    transaction_date é declarada por quem manda, e as janelas de histórico
    contam pelo relógio do servidor: uma data de meses atrás, ou de
    amanhã, não é um Pix de agora, e a RFC pede tolerância explícita (§2
    e §5). 400, erro contratual, e não veredito: nada fica gravado. Os
    limites estão no policy.py (TRANSACTION_DATE_MAX_AGE e
    TRANSACTION_DATE_MAX_SKEW), e a descrição diz os dois.
    """

    code = "QIT001004"

    def __init__(self, transaction_date, max_age_days, max_skew_minutes) -> None:
        title = "Transaction Date Out Of Tolerance"
        http_status = 400
        description = (
            f"transaction_date {transaction_date} is outside the accepted tolerance: "
            f"up to {max_age_days} days before and {max_skew_minutes} minutes after the server clock."
        )
        translation = (
            f"A data do Pix está fora da tolerância aceita: até {max_age_days} dias para trás "
            f"e {max_skew_minutes} minutos para a frente do relógio do servidor."
        )
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


class PixTransactionProcessing(QIException):
    """Este id está sendo avaliado neste instante, por esta ou por outra requisição.

    409 com Retry-After, e sem nenhum campo de sucesso: o recibo do id
    está em processing, com o lease vigente, e a RFC manda não iniciar um
    segundo avaliador (§8) — nem esperar pelo primeiro, que pode levar os
    prazos das três fontes. Quem chamou tenta de novo em instantes e
    recebe a análise gravada (200), ou o GET. Vale também pro GET que
    chega antes de a análise existir.
    """

    code = "QIT001006"

    def __init__(self, external_id, retry_after_seconds) -> None:
        title = "Pix Transaction Processing"
        http_status = 409
        description = f"Pix transaction with id {external_id} is being evaluated; retry in {retry_after_seconds} second(s)."
        translation = f"O Pix com id {external_id} está em avaliação; tente de novo em {retry_after_seconds} segundo(s)."
        super().__init__(
            title, self.code, http_status, description, translation,
            headers={"Retry-After": str(retry_after_seconds)},
        )


class PixTransactionEvaluationFailed(QIException):
    """O id chegou, o recibo existe, e a avaliação falhou antes de gravar a análise.

    O GET não forja sucesso nem diz que o Pix nunca existiu: 503, e o
    recibo failed continua lá, com o motivo. O mesmo Pix reenviado
    reivindica o recibo e é avaliado de novo (RFC §8); é o que resolve.
    """

    code = "QIT001007"

    def __init__(self, external_id) -> None:
        title = "Pix Transaction Evaluation Failed"
        http_status = 503
        description = f"Pix transaction with id {external_id} was received but its evaluation failed; resend it to be evaluated again."
        translation = f"O Pix com id {external_id} foi recebido, mas a avaliação falhou; reenvie para avaliar de novo."
        super().__init__(title, self.code, http_status, description, translation)
