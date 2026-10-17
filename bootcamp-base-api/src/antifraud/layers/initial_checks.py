"""Camada 1 — validações iniciais: o que o próprio Pix diz, sem olhar
histórico.

Idade das contas, horário e documentos vêm no payload, então os quatro
sinais daqui sempre respondem e nunca ficam unavailable. São os únicos
que valem no primeiro Pix de um cliente: o resto do motor ainda não tem
com o que comparar.
"""

from antifraud import policy
from antifraud.contract import Context, Signal


class InitialChecksLayer:
    def evaluate(self, context: Context) -> list[Signal]:
        return [
            _p01_new_payer_account(context),
            _l01_night_high_amount(context),
            _r01_new_recipient_account(context),
            _rel03_same_owner(context),
        ]


def _p01_new_payer_account(context: Context) -> Signal:
    age = context.transaction.payer_account_age_days
    if age < policy.NEW_ACCOUNT_DAYS:
        return policy.triggered("P01", {"account_age_days": age, "threshold": policy.NEW_ACCOUNT_DAYS})
    return policy.not_triggered("P01")


def _l01_night_high_amount(context: Context) -> Signal:
    transaction = context.transaction
    local = transaction.local_datetime
    # A janela atravessa a meia-noite, daí o or: das 20h às 23h59 de um
    # dia e da 0h às 5h59 do outro. Com and, nenhuma hora caberia.
    is_night = local.hour >= policy.NIGHT_START_HOUR or local.hour < policy.NIGHT_END_HOUR
    if is_night and transaction.amount > policy.NIGHT_AMOUNT_LIMIT:
        return policy.triggered("L01", {
            "local_time": local.strftime("%H:%M"),
            "amount": transaction.amount,
            "amount_threshold": policy.NIGHT_AMOUNT_LIMIT,
        })
    return policy.not_triggered("L01")


def _r01_new_recipient_account(context: Context) -> Signal:
    age = context.transaction.recipient_account_age_days
    if age < policy.NEW_ACCOUNT_DAYS:
        return policy.triggered("R01", {"account_age_days": age, "threshold": policy.NEW_ACCOUNT_DAYS})
    return policy.not_triggered("R01")


def _rel03_same_owner(context: Context) -> Signal:
    transaction = context.transaction
    if transaction.payer_document == transaction.recipient_document:
        return policy.triggered("REL03", {"same_document": True})
    return policy.not_triggered("REL03")
