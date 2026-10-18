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
    DictSecurityEvidence,
    EvidenceValue,
    Layer,
    RegistrationStatus,
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

# Quantos sinais as frases de nota (D-PAY, D-RCP e as rejeições por nota)
# citam, do mais pesado pro mais leve. Os outros continuam em
# metadata.signals.
_REASON_MAX_SIGNALS = 3

_CATALOG_POSITION = {code: position for position, code in enumerate(policy.CATALOG)}

# De que eixo é cada grupo de teto: todos os sinais de um grupo são do
# mesmo eixo, e o catálogo é quem sabe.
_GROUP_AXIS = {spec.cap_group: spec.axis for spec in policy.CATALOG.values()}


def evaluate(context: Context) -> Analysis:
    """Do Context à decisão explicada.

    Os sinais saem na ordem do catálogo, seja qual for a ordem do LAYERS.
    """
    signals: list[Signal] = []
    for layer in LAYERS:
        signals.extend(layer.evaluate(context))
    signals.sort(key=lambda signal: _CATALOG_POSITION[signal.code])

    triggered = {signal.code: signal for signal in signals if signal.status == SignalStatus.TRIGGERED}
    contributions = _family_contributions(triggered.values())
    payer_risk = _axis_risk(contributions, Axis.PAYER)
    recipient_risk = _axis_risk(contributions, Axis.RECIPIENT)
    rule = _decide(triggered.keys(), payer_risk, recipient_risk, context)

    return Analysis(
        status=rule.status,
        decision_rule=rule.name,
        reasons=(_reason(rule, triggered, payer_risk, recipient_risk, context),),
        payer_risk=payer_risk,
        recipient_risk=recipient_risk,
        payer_sufficiency=_sufficiency(context.payer.prior_attempts, context.payer.prior_days),
        recipient_sufficiency=_sufficiency(context.recipient.prior_attempts, context.recipient.prior_days),
        policy_version=policy.POLICY_VERSION,
        family_contributions=contributions,
        signals=tuple(signals),
    )


def _family_contributions(triggered: Iterable[Signal]) -> dict[str, dict[str, int]]:
    """A conta de cada grupo de teto: o que os sinais somaram (raw), o que entrou na nota (applied) e o teto.

    Os nove grupos saem sempre, na ordem do CAPS, mesmo com raw 0: a
    resposta mostra a conta inteira, e não só o que pesou.
    """
    raw = {group: 0 for group in policy.CAPS}
    for signal in triggered:
        raw[signal.cap_group] += signal.points
    return {
        group: {"raw": points, "applied": _clamp(points, *policy.CAPS[group]), "cap": policy.CAPS[group][1]}
        for group, points in raw.items()
    }


def _axis_risk(contributions: Mapping[str, Mapping[str, int]], axis: Axis) -> int:
    applied = sum(contribution["applied"] for group, contribution in contributions.items() if _GROUP_AXIS[group] is axis)
    return _clamp(applied, policy.MIN_RISK, policy.MAX_RISK)


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
    if rule.requires_restriction and not context.restrictions:
        return False
    if rule.requires_payer_adverse and not _adverse(context.payer_dict):
        return False
    if rule.requires_recipient_adverse and not _adverse(context.recipient_dict):
        return False
    if rule.requires_device_registry_unavailable and context.device_registration.status is not RegistrationStatus.UNAVAILABLE:
        return False
    return True


def _adverse(evidence: DictSecurityEvidence | None) -> bool:
    # DICT fora do ar não é evidência: unavailable nunca abre porteira de
    # rejeição (RFC §7).
    return evidence is not None and evidence.adverse


# Repare que as frases leem a evidência pelo nome da chave: renomear uma
# chave numa camada quebra o motivo aqui, com KeyError.
def _reason(
    rule: DecisionRule, triggered: Mapping[str, Signal], payer_risk: int, recipient_risk: int, context: Context,
) -> str:
    """A frase da linha da tabela que decidiu, com números da evidência.

    É uma frase só: os sinais, um a um, já estão em metadata.signals.
    """
    if rule.name == "D-RESTRICT-SIM":
        # Havendo mais de uma, a frase cita a primeira; todas vão em
        # metadata.restrictions.
        restriction = context.restrictions[0]
        return f"Restrição ativa: {restriction.reason} ({restriction.scope})"
    if rule.name == "D-REJ-ATO":
        return _risk_reason("do pagador", Axis.PAYER, payer_risk, rule.min_payer_risk, triggered)
    if rule.name == "D-REJ-PAY":
        risk = _risk_reason("do pagador", Axis.PAYER, payer_risk, rule.min_payer_risk, triggered)
        return f"{risk}, com evidência de fraude no DICT contra o pagador"
    if rule.name == "D-REJ-RCP":
        return _risk_reason("do destino", Axis.RECIPIENT, recipient_risk, rule.min_recipient_risk, triggered)
    if rule.name == "D-REJ-CROSS":
        return f"Risco cruzado (pagador {payer_risk}, destino {recipient_risk}) com evidência de fraude no DICT contra o destino"
    if rule.name == "D-ATO-1":
        ratio = triggered["P06"].evidence["ratio"]
        return f"Dispositivo não cadastrado no banco, com valor {_number(ratio)}x a mediana"
    if rule.name == "D-ATO-2":
        prior = triggered["P03"].evidence["payer_prior_tx"]
        return f"Dispositivo não cadastrado e IP sem Pix aprovado deste pagador, que tem {_number(prior)} Pix anteriores"
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
    if rule.name == "D-DATA-UNAVAILABLE":
        return "Cadastro de aparelhos indisponível: confirmação em vez de aprovar no escuro"
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
