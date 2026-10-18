"""O engine: roda as camadas, soma os pontos, aplica a tabela de decisão
e escreve o motivo.

É a única peça que olha sinais de camadas diferentes ao mesmo tempo, e
só na tabela de decisão. A porta de entrada é evaluate: o controller
monta o Context, chama evaluate(context) e grava a Analysis que volta.
"""

from collections.abc import Collection, Iterable, Mapping

from antifraud import policy
from antifraud.contract import (
    Analysis,
    Axis,
    Context,
    EvidenceValue,
    FraudMarker,
    Layer,
    Signal,
    SignalStatus,
    Sufficiency,
)
from antifraud.layers import DecisionTreeLayer, DictSecurityLayer, DictSecurityStatisticsLayer, InitialChecksLayer
from antifraud.policy import DecisionRule

# As camadas, na ordem em que rodam, uma por linha — como as cascas de
# middleware do app.py. A semelhança para aí: nenhuma camada interrompe
# a seguinte, do jeito que o 403 do token interrompe a requisição. As
# camadas rodam sempre, a resposta mostra todos os sinais do catálogo, e
# rejeição só sai da tabela de decisão. Desligar uma camada é apagar a
# linha dela, e as outras não percebem, porque nenhuma lê o sinal da
# outra. A ordem também não muda a resposta (os sinais saem na ordem do
# catálogo); ela segue a da RFC só pra leitura.
LAYERS: tuple[Layer, ...] = (
    InitialChecksLayer(),
    DecisionTreeLayer(),
    DictSecurityStatisticsLayer(),
    DictSecurityLayer(),
)

# Quantos sinais as frases de nota (D-PAY, D-RCP, D-REJ-ATO e D-REJ-RCP)
# citam, do mais pesado pro mais leve. Os outros continuam em
# metadata.signals.
_REASON_MAX_SIGNALS = 3

# As marcações do DICT que abrem a porteira das rejeições do destino.
_ADVERSE_MARKERS = (FraudMarker.SUSPICIOUS, FraudMarker.CONFIRMED)

_CATALOG_POSITION = {code: position for position, code in enumerate(policy.CATALOG)}


def evaluate(context: Context) -> Analysis:
    """Do Context à decisão explicada.

    Os sinais saem na ordem do catálogo, seja qual for a ordem do LAYERS.
    """
    signals: list[Signal] = []
    for layer in LAYERS:
        signals.extend(layer.evaluate(context))
    signals.sort(key=lambda signal: _CATALOG_POSITION[signal.code])

    triggered = {signal.code: signal for signal in signals if signal.status == SignalStatus.TRIGGERED}
    payer_risk = _axis_risk(triggered.values(), Axis.PAYER)
    recipient_risk = _axis_risk(triggered.values(), Axis.RECIPIENT)
    rule = _decide(triggered.keys(), payer_risk, recipient_risk, context)

    return Analysis(
        status=rule.status,
        decision_rule=rule.name,
        reasons=(_reason(rule, triggered, payer_risk, recipient_risk),),
        payer_risk=payer_risk,
        recipient_risk=recipient_risk,
        payer_sufficiency=_sufficiency(context.payer.prior_attempts, context.payer.prior_days),
        recipient_sufficiency=_sufficiency(context.recipient.prior_attempts, context.recipient.prior_days),
        policy_version=policy.POLICY_VERSION,
        signals=tuple(signals),
    )


def _axis_risk(triggered: Iterable[Signal], axis: Axis) -> int:
    group_points: dict[str, int] = {}
    for signal in triggered:
        if signal.axis == axis:
            group_points[signal.cap_group] = group_points.get(signal.cap_group, 0) + signal.points

    risk = 0
    for group, points in group_points.items():
        floor, ceiling = policy.CAPS[group]
        risk += _clamp(points, floor, ceiling)
    return _clamp(risk, policy.MIN_RISK, policy.MAX_RISK)


def _sufficiency(prior_attempts: int, prior_days: int) -> Sufficiency:
    if prior_attempts == 0:
        return Sufficiency.UNAVAILABLE
    if prior_attempts >= policy.HIGH_SUFFICIENCY_MIN_ATTEMPTS and prior_days >= policy.HIGH_SUFFICIENCY_MIN_DAYS:
        return Sufficiency.HIGH
    if prior_attempts >= policy.MEDIUM_SUFFICIENCY_MIN_ATTEMPTS and prior_days >= policy.MEDIUM_SUFFICIENCY_MIN_DAYS:
        return Sufficiency.MEDIUM
    return Sufficiency.LOW


def _decide(
    triggered: Collection[str], payer_risk: int, recipient_risk: int, context: Context,
) -> DecisionRule:
    for rule in policy.DECISION_TABLE:
        if _matches(rule, triggered, payer_risk, recipient_risk, context):
            return rule
    raise RuntimeError("a DECISION_TABLE precisa terminar numa linha sem condição, como o D-OK")


def _matches(
    rule: DecisionRule, triggered: Collection[str], payer_risk: int, recipient_risk: int,
    context: Context,
) -> bool:
    if any(code not in triggered for code in rule.all_triggered):
        return False
    if any(code in triggered for code in rule.none_triggered):
        return False
    if rule.min_payer_risk is not None and payer_risk < rule.min_payer_risk:
        return False
    if rule.min_recipient_risk is not None and recipient_risk < rule.min_recipient_risk:
        return False
    if rule.requires_recipient_adverse and context.dict_fraud_marker not in _ADVERSE_MARKERS:
        return False
    return True


# Repare que as frases leem a evidência pelo nome da chave: renomear uma
# chave numa camada quebra o motivo aqui, com KeyError.
def _reason(rule: DecisionRule, triggered: Mapping[str, Signal], payer_risk: int, recipient_risk: int) -> str:
    """A frase da linha da tabela que decidiu, com números da evidência.

    É uma frase só: os sinais, um a um, já estão em metadata.signals.
    """
    if rule.name == "D-REJ-ATO":
        return _risk_reason("do pagador", Axis.PAYER, payer_risk, rule.min_payer_risk, triggered)
    if rule.name == "D-REJ-RCP":
        return _risk_reason("do destino", Axis.RECIPIENT, recipient_risk, rule.min_recipient_risk, triggered)
    if rule.name == "D-REJ-CROSS":
        return f"Risco cruzado (pagador {payer_risk}, destino {recipient_risk}) com marcação de fraude no DICT"
    if rule.name == "D-ATO-1":
        ratio = triggered["P06"].evidence["ratio"]
        return f"Dispositivo sem Pix aprovado deste pagador, com valor {_number(ratio)}x a mediana"
    if rule.name == "D-ATO-2":
        prior = triggered["P03"].evidence["payer_prior_tx"]
        return f"Dispositivo e IP sem Pix aprovado deste pagador, que tem {_number(prior)} Pix anteriores"
    if rule.name == "D-PAY":
        return _risk_reason("do pagador", Axis.PAYER, payer_risk, rule.min_payer_risk, triggered)
    if rule.name == "D-LIMIT":
        evidence = triggered["L03"].evidence
        # Os valores da evidência estão em centavos; a frase sai em reais.
        reason = f"Valor de R$ {_number(evidence['amount'] / 100)} alcança o teto por Pix de R$ {_number(evidence['cap'] / 100)}"
        if evidence["payer_approved_count"] == 0:
            reason += " de quem nunca teve Pix aprovado"
        return reason
    if rule.name == "D-SCAM":
        ratio = triggered["P06"].evidence["ratio"]
        return f"Valor {_number(ratio)}x a mediana para um destino que o pagador nunca pagou"
    if rule.name == "D-DRAIN":
        attempts = triggered["P04"].evidence["attempts_5m"]
        destinations = triggered["P07"].evidence["new_destinations_10m"]
        return f"{_number(attempts)} tentativas em 5 min e {_number(destinations)} destinos inéditos em 10 min"
    if rule.name == "D-RCP":
        return _risk_reason("do destino", Axis.RECIPIENT, recipient_risk, rule.min_recipient_risk, triggered)
    if rule.name == "D-OK":
        return (
            "Nenhum indício pesou o bastante para pedir 2FA "
            f"(risco do pagador {payer_risk}, do destino {recipient_risk})"
        )
    raise RuntimeError(f"a linha {rule.name} da DECISION_TABLE não tem frase")


def _risk_reason(
    owner: str, axis: Axis, risk: int, threshold: int | None, triggered: Mapping[str, Signal],
) -> str:
    # Os que mais pesaram: só os que somaram ponto neste eixo, do maior
    # pro menor, e no empate fica a ordem do catálogo (o sorted não troca
    # iguais de lugar). Os pontos ficam fora da frase: com teto, a soma
    # deles não bate com a nota.
    heaviest = sorted(
        (signal for signal in triggered.values() if signal.axis == axis and signal.points > 0),
        key=lambda signal: -signal.points,
    )[:_REASON_MAX_SIGNALS]
    cited = [f"{_mid_sentence(policy.signal_spec(signal.code).name)} ({signal.code})" for signal in heaviest]
    if threshold is not None:
        qualifier = f"acima do limiar de {threshold}"
    else:
        qualifier = "com evidência suficiente"
    return f"Risco {owner} em {risk} ({qualifier}): {_join(cited)}"


def _mid_sentence(name: str) -> str:
    # O nome do catálogo começa com maiúscula, que cai no meio da frase;
    # sigla fica como está (IP continua IP).
    if name[1:2].islower():
        return name[0].lower() + name[1:]
    return name


def _join(items: list[str]) -> str:
    if len(items) < 2:
        return "".join(items)
    return f"{', '.join(items[:-1])} e {items[-1]}"


def _number(value: EvidenceValue) -> str:
    # Do jeito brasileiro: ponto no milhar, vírgula na decimal, e 6 em vez
    # de 6,0 (10.000 e 6,2).
    text = f"{value:,.2f}".rstrip("0").rstrip(".")
    return text.replace(",", "_").replace(".", ",").replace("_", ".")


def _clamp(value: int, floor: int, ceiling: int) -> int:
    return max(floor, min(value, ceiling))
