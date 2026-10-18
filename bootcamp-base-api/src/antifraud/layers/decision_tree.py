"""Camada 2 — árvore de decisão: as regras que comparam o Pix com o
histórico.

Cadê a árvore? Cada regra é um galho de dois níveis: tem histórico pra
responder? Se não tem, unavailable. Se tem, a condição vale? As 14
rodam sempre, sem atalho de uma pra outra, porque o GET precisa mostrar
o estado de cada uma. O tronco, que junta os galhos numa decisão, é a
DECISION_TABLE do policy.py. É `if/else` escrito à mão, e não modelo
treinado: sem dado rotulado não há o que treinar, e cada resposta
precisa caber numa frase.

As contagens do Context são dos Pix anteriores. Quem precisa do Pix
atual na conta (L02, P04, P05, R02) soma o atual aqui, uma vez.
"""

import math

from antifraud import policy
from antifraud.contract import Context, Signal


class DecisionTreeLayer:
    def evaluate(self, context: Context) -> list[Signal]:
        return [
            _l02_new_device_over_limit(context),
            _p03_unknown_device(context),
            _p04_many_attempts(context),
            _p05_high_hourly_amount(context),
            _p06_atypical_amount(context),
            _p07_many_new_destinations(context),
            _p08_shared_device(context),
            _p09_unknown_ip(context),
            _p10_unknown_device_and_ip(context),
            _r02_many_entries(context),
            _r03_many_payers(context),
            _r04_new_account_many_payers(context),
            _rel01_new_relation(context),
            _rel02_recurrent_relation(context),
        ]


# As perguntas de histórico, uma por função. A primeira serve a L02,
# P03, P09 e P10 e conta Pix aprovados, como o device_known e o
# ip_known: sem 3 aprovados não há aparelho nem IP conhecido a perder.
# É uma função só por causa do P10, que fica unavailable quando o P03 ou
# o P09 ficam: com a mesma função os dois não têm como divergir.
def _short_access_history(context: Context) -> bool:
    return context.payer.approved_count < policy.ACCESS_MIN_APPROVED


def _payer_is_new(context: Context) -> bool:
    return context.payer.prior_attempts == 0


def _device_is_new(context: Context) -> bool:
    return context.device.prior_attempts == 0


def _recipient_is_new(context: Context) -> bool:
    return context.recipient.prior_attempts == 0


def _l02_new_device_over_limit(context: Context) -> Signal:
    if context.device_trusted is None:
        return policy.unavailable("L02")
    # A RFC escreve "P03 disparado e (valor > R$ 200 ou soma do dia >
    # R$ 1.000)". Com o P03 da RFC ao pé da letra (aparelho sem registro
    # nenhum com o pagador), a soma do dia seria sempre o próprio Pix, e
    # a segunda metade nunca decidiria nada. Aqui aparelho novo é o que
    # não tinha Pix aprovado antes de hoje: o limite do Banco Central pra
    # dispositivo não cadastrado é diário, e assim o segundo e o terceiro
    # Pix do dia no aparelho novo também entram na soma.
    payer = context.payer
    amount = context.transaction.amount
    day_amount = payer.unregistered_device_day_amount + amount
    over_limit = amount > policy.NEW_DEVICE_PIX_LIMIT or day_amount > policy.NEW_DEVICE_DAY_LIMIT
    if context.device_trusted is False and over_limit:
        return policy.triggered("L02", {
            "amount": amount,
            "amount_threshold": policy.NEW_DEVICE_PIX_LIMIT,
            "device_day_amount": day_amount,
            "device_day_amount_threshold": policy.NEW_DEVICE_DAY_LIMIT,
            "device_seen_before_today": False,
        })
    return policy.not_triggered("L02")


def _p03_unknown_device(context: Context) -> Signal:
    if context.device_trusted is None:
        return policy.unavailable("P03")
    if not context.device_trusted:
        return policy.triggered("P03", {"device_registered": False, "device_seen_before": False})
    return policy.not_triggered("P03")


def _p04_many_attempts(context: Context) -> Signal:
    if _payer_is_new(context):
        return policy.unavailable("P04")
    attempts = context.payer.attempts_5m + 1
    if attempts >= policy.PAYER_ATTEMPTS_THRESHOLD:
        return policy.triggered("P04", {"attempts_5m": attempts, "threshold": policy.PAYER_ATTEMPTS_THRESHOLD})
    return policy.not_triggered("P04")


def _p05_high_hourly_amount(context: Context) -> Signal:
    if _payer_is_new(context):
        return policy.unavailable("P05")
    amount_1h = context.payer.amount_1h + context.transaction.amount
    if amount_1h > policy.PAYER_AMOUNT_LIMIT:
        return policy.triggered("P05", {"amount_1h": amount_1h, "threshold": policy.PAYER_AMOUNT_LIMIT})
    return policy.not_triggered("P05")


def _p06_atypical_amount(context: Context) -> Signal:
    payer = context.payer
    if payer.approved_count < policy.MEDIAN_MIN_APPROVED or payer.approved_days < policy.MEDIAN_MIN_DAYS:
        return policy.unavailable("P06")
    # Abaixo do piso o P06 não dispara, seja qual for a mediana. A
    # pergunta vem antes da mediana pra que o controller possa nem
    # calculá-la nesse caso: o percentile_cont ordena todos os Pix
    # aprovados do pagador.
    amount = context.transaction.amount
    if amount < policy.ATYPICAL_MIN_AMOUNT:
        return policy.not_triggered("P06")
    # Mediana, e não média: um Pix de R$ 5.000 no meio de vinte de R$ 50
    # puxa a média pra R$ 286 e deixa a mediana nos R$ 50. Sem mediana
    # nenhuma não há com o que comparar, e conferir o zero impede a
    # divisão da evidência de explodir.
    median = payer.approved_median_amount
    if median is None or median <= 0:
        return policy.unavailable("P06")
    if amount > policy.MEDIAN_MULTIPLIER * median:
        return policy.triggered("P06", {
            "amount": amount,
            # O percentile_cont devolve float; 100000.0 sai como 100000.
            "median_amount": int(median) if median == int(median) else median,
            # Pra cima, em 2 casas: quem disparou com 5,001x aparece como
            # 5,01x, e nunca como 5x, que não dispararia.
            "ratio": math.ceil(amount * 100 / median) / 100,
            "ratio_threshold": policy.MEDIAN_MULTIPLIER,
            "amount_threshold": policy.ATYPICAL_MIN_AMOUNT,
        })
    return policy.not_triggered("P06")


def _p07_many_new_destinations(context: Context) -> Signal:
    if _payer_is_new(context):
        return policy.unavailable("P07")
    destinations = context.payer.new_destinations_10m
    if destinations >= policy.NEW_DESTINATIONS_THRESHOLD:
        return policy.triggered("P07", {
            "new_destinations_10m": destinations,
            "threshold": policy.NEW_DESTINATIONS_THRESHOLD,
        })
    return policy.not_triggered("P07")


def _p08_shared_device(context: Context) -> Signal:
    if _device_is_new(context):
        return policy.unavailable("P08")
    payers = context.device.distinct_payers_24h
    if payers >= policy.SHARED_DEVICE_THRESHOLD:
        return policy.triggered("P08", {"distinct_payers_24h": payers, "threshold": policy.SHARED_DEVICE_THRESHOLD})
    return policy.not_triggered("P08")


def _p09_unknown_ip(context: Context) -> Signal:
    if _short_access_history(context):
        return policy.unavailable("P09")
    payer = context.payer
    if not payer.ip_known:
        return policy.triggered("P09", {"payer_prior_tx": payer.prior_attempts, "ip_seen_before": False})
    return policy.not_triggered("P09")


def _p10_unknown_device_and_ip(context: Context) -> Signal:
    if _short_access_history(context):
        return policy.unavailable("P10")
    payer = context.payer
    if not payer.device_known and not payer.ip_known:
        return policy.triggered("P10", {
            "payer_prior_tx": payer.prior_attempts,
            "device_seen_before": False,
            "ip_seen_before": False,
        })
    return policy.not_triggered("P10")


def _r02_many_entries(context: Context) -> Signal:
    if _recipient_is_new(context):
        return policy.unavailable("R02")
    attempts = context.recipient.attempts_10m + 1
    if attempts >= policy.RECIPIENT_ATTEMPTS_THRESHOLD:
        return policy.triggered("R02", {"attempts_10m": attempts, "threshold": policy.RECIPIENT_ATTEMPTS_THRESHOLD})
    return policy.not_triggered("R02")


def _r03_many_payers(context: Context) -> Signal:
    if _recipient_is_new(context):
        return policy.unavailable("R03")
    payers = context.recipient.distinct_payers_1h
    if payers >= policy.RECIPIENT_PAYERS_THRESHOLD:
        return policy.triggered("R03", {"distinct_payers_1h": payers, "threshold": policy.RECIPIENT_PAYERS_THRESHOLD})
    return policy.not_triggered("R03")


def _r04_new_account_many_payers(context: Context) -> Signal:
    if _recipient_is_new(context):
        return policy.unavailable("R04")
    # Conta nova é a condição do R01, que mora na camada 1. Ela é refeita
    # aqui, a partir do Context, e não lida do sinal do R01: o porquê
    # está no Layer, em contract.py.
    age = context.transaction.recipient_account_age_days
    payers = context.recipient.distinct_payers_1h
    if age < policy.NEW_ACCOUNT_DAYS and payers >= policy.RECIPIENT_PAYERS_THRESHOLD:
        return policy.triggered("R04", {
            "account_age_days": age,
            "account_age_days_threshold": policy.NEW_ACCOUNT_DAYS,
            "distinct_payers_1h": payers,
            "distinct_payers_1h_threshold": policy.RECIPIENT_PAYERS_THRESHOLD,
        })
    return policy.not_triggered("R04")


def _rel01_new_relation(context: Context) -> Signal:
    if _payer_is_new(context):
        return policy.unavailable("REL01")
    if context.pair.prior_approved == 0:
        return policy.triggered("REL01", {"pair_prior_approved": 0})
    return policy.not_triggered("REL01")


def _rel02_recurrent_relation(context: Context) -> Signal:
    if _payer_is_new(context):
        return policy.unavailable("REL02")
    # Uma rejeição ou um 2FA no par nos últimos 7 dias suspende o
    # desconto: se o próprio motor desconfiou desse par há pouco,
    # descontar agora facilitaria justo a tentativa seguinte.
    pair = context.pair
    recurrent = (
        pair.prior_approved >= policy.RECURRENT_PAIR_MIN_APPROVED
        and pair.first_approved_days_ago is not None
        and pair.first_approved_days_ago >= policy.RECURRENT_PAIR_MIN_DAYS
        and not pair.recent_anomaly
    )
    if recurrent:
        return policy.triggered("REL02", {
            "pair_prior_approved": pair.prior_approved,
            "pair_prior_approved_threshold": policy.RECURRENT_PAIR_MIN_APPROVED,
            "first_approved_days_ago": pair.first_approved_days_ago,
            "first_approved_days_ago_threshold": policy.RECURRENT_PAIR_MIN_DAYS,
            "recent_anomaly": False,
        })
    return policy.not_triggered("REL02")
