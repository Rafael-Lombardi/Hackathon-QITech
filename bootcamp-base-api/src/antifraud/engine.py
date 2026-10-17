"""O engine: roda as camadas, soma os pontos, aplica a tabela de decisão e
escreve os motivos.

É a única peça que olha sinais de camadas diferentes ao mesmo tempo, e só
na tabela de decisão. A porta de entrada é evaluate: o controller monta o
Context, chama evaluate(context) e grava a Analysis que volta.
"""

from collections.abc import Collection, Iterable, Mapping

from antifraud import policy
from antifraud.contract import Analysis, Axis, Context, EvidenceValue, Layer, Signal, SignalStatus, Sufficiency
from antifraud.layers import DecisionTreeLayer, InitialChecksLayer, TrustFactorLayer
from antifraud.policy import DecisionRule

# As camadas, na ordem em que rodam, uma por linha — como as cascas de
# middleware do app.py. Desligar uma camada é apagar a linha dela, e as
# outras não percebem: nenhuma lê o sinal da outra. Diferente dos
# middlewares, aqui a ordem não muda a resposta, porque os sinais saem na
# ordem do catálogo de qualquer jeito; ela segue a da RFC só pra leitura.
LAYERS: tuple[Layer, ...] = (
    InitialChecksLayer(),
    DecisionTreeLayer(),
    TrustFactorLayer(),
)

# Quantos sinais a primeira frase do D-PAY e do D-RCP cita. Os outros que
# dispararam vêm nas frases seguintes, um por frase.
_HEAVIEST_SIGNALS = 3

_CATALOG_POSITION = {code: position for position, code in enumerate(policy.CATALOG)}


def evaluate(context: Context) -> Analysis:
    """Do Context à decisão explicada. Os sinais saem na ordem do catálogo,
    seja qual for a ordem do LAYERS."""
    signals: list[Signal] = []
    for layer in LAYERS:
        signals.extend(layer.evaluate(context))
    signals.sort(key=lambda signal: _CATALOG_POSITION[signal.code])

    triggered = {signal.code: signal for signal in signals if signal.status == SignalStatus.TRIGGERED}
    payer_risk = _axis_risk(triggered.values(), Axis.PAYER)
    recipient_risk = _axis_risk(triggered.values(), Axis.RECIPIENT)
    rule = _decide(triggered.keys(), payer_risk, recipient_risk)

    return Analysis(
        status=rule.status,
        decision_rule=rule.name,
        reasons=_reasons(rule, triggered, payer_risk, recipient_risk),
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


def _decide(triggered: Collection[str], payer_risk: int, recipient_risk: int) -> DecisionRule:
    for rule in policy.DECISION_TABLE:
        if _matches(rule, triggered, payer_risk, recipient_risk):
            return rule
    raise RuntimeError("a DECISION_TABLE precisa terminar numa linha sem condição, como a D-OK")


def _matches(rule: DecisionRule, triggered: Collection[str], payer_risk: int, recipient_risk: int) -> bool:
    if any(code not in triggered for code in rule.all_triggered):
        return False
    if rule.min_payer_risk is not None and payer_risk < rule.min_payer_risk:
        return False
    if rule.min_recipient_risk is not None and recipient_risk < rule.min_recipient_risk:
        return False
    return True


def _reasons(
    rule: DecisionRule, triggered: Mapping[str, Signal], payer_risk: int, recipient_risk: int,
) -> tuple[str, ...]:
    """A primeira frase explica a linha que decidiu, com números da
    evidência; as seguintes nomeiam os outros sinais que dispararam, na
    ordem do catálogo. A ordem nunca sai de um set, e a mesma análise tem
    sempre as mesmas frases."""
    first, cited = _decisive_reason(rule, triggered, payer_risk, recipient_risk)
    others = [_describe(signal) for code, signal in triggered.items() if code not in cited]
    return (first, *others)


def _decisive_reason(
    rule: DecisionRule, triggered: Mapping[str, Signal], payer_risk: int, recipient_risk: int,
) -> tuple[str, Collection[str]]:
    if rule.name == "D-REJ":
        age = triggered["R01"].evidence["account_age_days"]
        payers = triggered["R03"].evidence["distinct_payers_1h"]
        attempts = triggered["R02"].evidence["attempts_10m"]
        text = (
            f"Conta destino aberta {_opened(age)} recebendo de {payers} pagadores distintos "
            f"na última hora e {attempts} tentativas em 10 min"
        )
        return text, rule.all_triggered
    if rule.name == "D-ATO-1":
        ratio = triggered["P06"].evidence["ratio"]
        return f"Dispositivo nunca visto para este pagador com valor {_decimal(ratio)}x acima da mediana", rule.all_triggered
    if rule.name == "D-ATO-2":
        prior = triggered["P03"].evidence["payer_prior_tx"]
        return f"Dispositivo e IP nunca vistos para este pagador, que tem {prior} Pix anteriores", rule.all_triggered
    if rule.min_payer_risk is not None:
        return _risk_reason(Axis.PAYER, payer_risk, rule.min_payer_risk, triggered)
    if rule.min_recipient_risk is not None:
        return _risk_reason(Axis.RECIPIENT, recipient_risk, rule.min_recipient_risk, triggered)
    if rule.all_triggered:
        # Linha de combinação que ainda não ganhou frase própria. Sem isto,
        # ela cairia na frase do D-OK, que diria o contrário do que houve.
        names = " e ".join(policy.CATALOG[code].name for code in rule.all_triggered)
        return f"{names}, ao mesmo tempo", rule.all_triggered
    return "Nenhum indício justificou fricção", ()


def _risk_reason(
    axis: Axis, risk: int, threshold: int, triggered: Mapping[str, Signal],
) -> tuple[str, Collection[str]]:
    # Os que mais pesaram: só os que somaram ponto neste eixo, do maior pro
    # menor. No empate fica a ordem do catálogo — o sorted não troca iguais
    # de lugar.
    heaviest = sorted(
        (signal for signal in triggered.values() if signal.axis == axis and signal.points > 0),
        key=lambda signal: -signal.points,
    )[:_HEAVIEST_SIGNALS]
    owner = "do pagador" if axis == Axis.PAYER else "do destino"
    listed = ", ".join(_describe(signal) for signal in heaviest)
    return f"Risco {owner} {risk} (2FA a partir de {threshold}): {listed}", [signal.code for signal in heaviest]


def _describe(signal: Signal) -> str:
    return f"{policy.CATALOG[signal.code].name} ({signal.code}, {signal.points:+d})"


def _opened(days: EvidenceValue) -> str:
    if days == 0:
        return "hoje"
    if days == 1:
        return "há 1 dia"
    return f"há {days} dias"


def _decimal(value: EvidenceValue) -> str:
    return str(value).replace(".", ",")


def _clamp(value: int, floor: int, ceiling: int) -> int:
    return max(floor, min(value, ceiling))
