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
    Layer,
    Signal,
    SignalStatus,
    Sufficiency,
)
from antifraud.layers import DecisionTreeLayer, InitialChecksLayer, TrustFactorLayer
from antifraud.policy import DecisionRule

# As camadas, na ordem em que rodam, uma por linha — como as cascas de
# middleware do app.py. A semelhança para aí: nenhuma camada interrompe
# a seguinte, do jeito que o 403 do token interrompe a requisição. As
# três rodam sempre, a resposta mostra os 19 sinais, e rejeição só sai
# da tabela de decisão. Desligar uma camada é apagar a linha dela, e as
# outras não percebem, porque nenhuma lê o sinal da outra. A ordem
# também não muda a resposta (os sinais saem na ordem do catálogo); ela
# segue a da RFC só pra leitura.
LAYERS: tuple[Layer, ...] = (
    InitialChecksLayer(),
    DecisionTreeLayer(),
    TrustFactorLayer(),
)

# Quantos sinais a frase do D-PAY e do D-RCP cita, do mais pesado pro
# mais leve. Os outros continuam em metadata.signals.
_REASON_MAX_SIGNALS = 3

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
    rule = _decide(triggered.keys(), payer_risk, recipient_risk)

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


# Repare que as frases leem a evidência pelo nome da chave: renomear uma
# chave numa camada quebra o motivo aqui, com KeyError.
def _reason(rule: DecisionRule, triggered: Mapping[str, Signal], payer_risk: int, recipient_risk: int) -> str:
    """A frase da linha da tabela que decidiu, com números da evidência.

    É uma frase só: os sinais, um a um, já estão em metadata.signals.
    """
    if rule.name == "D-REJ":
        age = triggered["R01"].evidence["account_age_days"]
        payers = triggered["R03"].evidence["distinct_payers_1h"]
        attempts = triggered["R02"].evidence["attempts_10m"]
        return (
            f"Conta destino aberta {_opened(age)}, com {_number(payers)} pagadores distintos "
            f"na última hora e {_number(attempts)} tentativas em 10 min"
        )
    if rule.name == "D-ATO-1":
        ratio = triggered["P06"].evidence["ratio"]
        return f"Dispositivo sem Pix aprovado deste pagador, com valor {_number(ratio)}x a mediana"
    if rule.name == "D-ATO-2":
        prior = triggered["P03"].evidence["payer_prior_tx"]
        return f"Dispositivo e IP sem Pix aprovado deste pagador, que tem {_number(prior)} Pix anteriores"
    if rule.name == "D-PAY":
        return _risk_reason("do pagador", Axis.PAYER, payer_risk, rule.min_payer_risk, triggered)
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
    return f"Risco {owner} em {risk} (2FA a partir de {threshold}): {_join(cited)}"


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


def _opened(days: EvidenceValue) -> str:
    if days == 0:
        return "hoje"
    if days == 1:
        return "há 1 dia"
    return f"há {_number(days)} dias"


def _number(value: EvidenceValue) -> str:
    # Do jeito brasileiro: ponto no milhar, vírgula na decimal, e 6 em vez
    # de 6,0 (10.000 e 6,2).
    text = f"{value:,.2f}".rstrip("0").rstrip(".")
    return text.replace(",", "_").replace(".", ",").replace("_", ".")


def _clamp(value: int, floor: int, ceiling: int) -> int:
    return max(floor, min(value, ceiling))
