"""Camada 2 — árvore de decisão: as regras que comparam o Pix com o
histórico e com o cadastro de aparelhos do banco.

Cadê a árvore? Cada regra é um galho de dois níveis: tem histórico pra
responder? Se não tem, unavailable. Se tem, a condição vale? Todas
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
from antifraud.contract import Context, Evidence, RegistrationStatus, Signal


class DecisionTreeLayer:
    def evaluate(self, context: Context) -> list[Signal]:
        return [
            _l02_unregistered_device_over_limit(context),
            _l03_amount_cap(context),
            _g01_unusual_location(context),
            _p03_unregistered_device(context),
            _p04_many_attempts(context),
            _p05_high_hourly_amount(context),
            _p06_atypical_amount(context),
            _p07_many_new_destinations(context),
            _p08_shared_device(context),
            _p09_unknown_ip(context),
            _p10_unregistered_device_and_unknown_ip(context),
            _r02_many_entries(context),
            _r03_many_payers(context),
            _r04_new_account_many_payers(context),
            _rel01_new_relation(context),
            _rel02_recurrent_relation(context),
        ]


# G01: a distância até o centro habitual mais perto vem pronta do
# controller — None quando não há padrão (poucas observações, ou nenhum
# centro com 2 dias) ou quando o Pix veio sem localização. Os números e o
# porquê estão nas LOCATION_* do policy.py.
def _g01_unusual_location(context: Context) -> Signal:
    distance = context.transaction.location_distance_km
    if distance is None:
        return policy.unavailable("G01")
    if distance > policy.LOCATION_DISTANCE_THRESHOLD_KM:
        return policy.triggered("G01", {
            "distance_km": round(distance, 2),
            "threshold_km": policy.LOCATION_DISTANCE_THRESHOLD_KM,
        })
    return policy.not_triggered("G01")


# As perguntas de aparelho e de IP, uma por função, e as de histórico,
# logo abaixo. As duas primeiras respondem em três estados — sim, não e
# "não sei" (None) — porque três sinais as leem (P03, L02 e P10 pro
# aparelho; P09 e P10 pro IP) e o P10 fica unavailable quando qualquer
# uma das duas fica: com a mesma função, os sinais não têm como divergir.
#
# O aparelho é o que o cadastro do banco diz. Pagador fora da fixture do
# cadastro ou fonte fora do ar é "não sei": o motor não lê ausência como
# não-cadastro (o porquê está em RegistrationStatus). O IP é o que o
# histórico diz, e só vale com 3 Pix aprovados (ACCESS_MIN_APPROVED).
def _device_unregistered(context: Context) -> bool | None:
    status = context.device_registration.status
    if status is RegistrationStatus.UNREGISTERED:
        return True
    if status is RegistrationStatus.REGISTERED:
        return False
    return None


def _ip_unknown(context: Context) -> bool | None:
    if context.payer.approved_count < policy.ACCESS_MIN_APPROVED:
        return None
    return not context.payer.ip_known


# O que o P03 e o P10 contam do aparelho: o que o cadastro respondeu, de
# onde veio, se o par já tinha aparecido e quanto histórico o pagador
# tem — os nomes do exemplo da RFC.
def _device_evidence(context: Context) -> Evidence:
    registration = context.device_registration
    evidence: Evidence = {"registration_status": registration.status.value}
    if registration.source is not None:
        evidence["registration_source"] = registration.source
    evidence["previously_observed"] = registration.previously_observed
    evidence["payer_prior_tx"] = context.payer.prior_attempts
    return evidence


def _payer_is_new(context: Context) -> bool:
    return context.payer.prior_attempts == 0


def _device_is_new(context: Context) -> bool:
    return context.device.prior_attempts == 0


def _recipient_is_new(context: Context) -> bool:
    return context.recipient.prior_attempts == 0


def _l02_unregistered_device_over_limit(context: Context) -> Signal:
    unregistered = _device_unregistered(context)
    if unregistered is None:
        return policy.unavailable("L02")
    # "P03 disparado e (valor > R$ 200 ou soma do dia > R$ 1.000)", como a
    # RFC escreve. A soma do dia é a de todas as tentativas de hoje feitas
    # de aparelho sem cadastro, de qualquer status, mais esta: o limite
    # diário do Banco Central é por cliente, e o invasor com dois
    # celulares não divide o teto entre eles.
    amount = context.transaction.amount
    day_amount = context.payer.unregistered_device_day_amount + amount
    over_limit = amount > policy.NEW_DEVICE_PIX_LIMIT or day_amount > policy.NEW_DEVICE_DAY_LIMIT
    if unregistered and over_limit:
        return policy.triggered("L02", {
            "amount": amount,
            "amount_threshold": policy.NEW_DEVICE_PIX_LIMIT,
            "unregistered_device_day_amount": day_amount,
            "unregistered_device_day_amount_threshold": policy.NEW_DEVICE_DAY_LIMIT,
        })
    return policy.not_triggered("L02")


def _l03_amount_cap(context: Context) -> Signal:
    # Nunca unavailable: o teto vale desde o primeiro Pix, e é justamente
    # no primeiro que ele é mais baixo — com 0 aprovados na base, R$ 5.000;
    # com algum, R$ 50.000. O porquê dos dois números está no policy.py.
    transaction = context.transaction
    if transaction.payer_document == transaction.recipient_document:
        return policy.not_triggered("L03")
    approved_count = context.payer.approved_count
    cap = policy.PIX_AMOUNT_CAP if approved_count > 0 else policy.COLD_START_AMOUNT_CAP
    if transaction.amount >= cap:
        return policy.triggered("L03", {
            "amount": transaction.amount,
            "cap": cap,
            "payer_approved_count": approved_count,
        })
    return policy.not_triggered("L03")


def _p03_unregistered_device(context: Context) -> Signal:
    unregistered = _device_unregistered(context)
    if unregistered is None:
        return policy.unavailable("P03")
    if unregistered:
        return policy.triggered("P03", _device_evidence(context))
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
    ip_unknown = _ip_unknown(context)
    if ip_unknown is None:
        return policy.unavailable("P09")
    if ip_unknown:
        return policy.triggered("P09", {"payer_prior_tx": context.payer.prior_attempts, "ip_seen_before": False})
    return policy.not_triggered("P09")


def _p10_unregistered_device_and_unknown_ip(context: Context) -> Signal:
    unregistered, ip_unknown = _device_unregistered(context), _ip_unknown(context)
    if unregistered is None or ip_unknown is None:
        return policy.unavailable("P10")
    if unregistered and ip_unknown:
        return policy.triggered("P10", {**_device_evidence(context), "ip_seen_before": False})
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
    # Um sinal adverso numa tentativa do par nas últimas 24 h, ou uma
    # restrição ativa agora, suspende o desconto: se o próprio motor
    # desconfiou desse par há pouco, descontar agora facilitaria justo a
    # tentativa seguinte (RFC §5). Com restrição a resposta é rejeição de
    # qualquer jeito; o sinal fica calado pra contar a história certa.
    pair = context.pair
    recurrent = (
        pair.prior_approved >= policy.RECURRENT_PAIR_MIN_APPROVED
        and pair.first_approved_days_ago is not None
        and pair.first_approved_days_ago >= policy.RECURRENT_PAIR_MIN_DAYS
        and not pair.recent_anomaly
        and not context.restrictions
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
