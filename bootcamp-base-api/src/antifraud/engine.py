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
    PixStatus,
    Signal,
    SignalStatus,
    Sufficiency,
)
from antifraud.layers import DecisionTreeLayer, DictSecurityLayer, InitialChecksLayer, DictSecurityStatisticsLayer
from antifraud.policy import DecisionRule

# As camadas, na ordem em que rodam, uma por linha — como as cascas de
# middleware do app.py. A semelhança para aí: nenhuma camada interrompe
# a seguinte, do jeito que o 403 do token interrompe a requisição. As
# as camadas rodam sempre, a resposta mostra os 22 sinais, e rejeição só sai
# da tabela de decisão. Desligar uma camada é apagar a linha dela, e as
# outras não percebem, porque nenhuma lê o sinal da outra. A ordem
# também não muda a resposta (os sinais saem na ordem do catálogo); ela
# segue a da RFC só pra leitura.
LAYERS: tuple[Layer, ...] = (
    InitialChecksLayer(),
    DecisionTreeLayer(),
    DictSecurityStatisticsLayer(),
    DictSecurityLayer(),
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

    if not context.restriction_available:
        raise RuntimeError("fonte de restrições indisponível; análise sem veredicto")

    triggered = {signal.code: signal for signal in signals if signal.status == SignalStatus.TRIGGERED}
    family_contributions = _family_contributions(triggered.values())
    payer_risk = _axis_risk(family_contributions, Axis.PAYER)
    recipient_risk = _axis_risk(family_contributions, Axis.RECIPIENT)
    rule = _decide(triggered.keys(), payer_risk, recipient_risk, context)

    # Indisponibilidade só é fallback: não pode apagar restrição ou
    # rejeição comprovável por outras fontes. Sinais dependentes do cadastro
    # continuam unavailable e impedem as regras que exigem esses sinais.
    if not context.device_registry_available and rule.name == "D-OK":
        return Analysis(
            status=PixStatus.TWO_FACTOR_REQUIRED,
            decision_rule="D-DATA-UNAVAILABLE",
            reasons=("Cadastro de dispositivos indisponível; 2FA obrigatório",),
            payer_risk=payer_risk,
            recipient_risk=recipient_risk,
            payer_sufficiency=_sufficiency(context.payer.prior_attempts, context.payer.prior_days),
            recipient_sufficiency=_sufficiency(context.recipient.prior_attempts, context.recipient.prior_days),
            policy_version=policy.POLICY_VERSION,
            family_contributions=family_contributions,
            signals=tuple(signals),
        )

    return Analysis(
        status=rule.status,
        decision_rule=rule.name,
        reasons=(_reason(rule, triggered, payer_risk, recipient_risk),),
        payer_risk=payer_risk,
        recipient_risk=recipient_risk,
        payer_sufficiency=_sufficiency(context.payer.prior_attempts, context.payer.prior_days),
        recipient_sufficiency=_sufficiency(context.recipient.prior_attempts, context.recipient.prior_days),
        policy_version=policy.POLICY_VERSION,
        family_contributions=family_contributions,
        signals=tuple(signals),
    )


def _family_contributions(triggered: Iterable[Signal]) -> dict[str, dict[str, int | str]]:
    group_points = {group: 0 for group in policy.CAPS}
    for signal in triggered:
        group_points[signal.cap_group] += signal.points

    return {
        group: {
            "axis": next(spec.axis.value for spec in policy.CATALOG.values() if spec.cap_group == group),
            "raw": raw,
            "applied": _clamp(raw, floor, ceiling),
            "floor": floor,
            "cap": ceiling,
        }
        for group, raw in group_points.items()
        for floor, ceiling in (policy.CAPS[group],)
    }


def _axis_risk(contributions: Mapping[str, Mapping[str, int | str]], axis: Axis) -> int:
    return _clamp(
        sum(int(group["applied"]) for group in contributions.values() if group["axis"] == axis.value),
        policy.MIN_RISK,
        policy.MAX_RISK,
    )


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
        if rule.name == "D-RESTRICT-SIM" and not context.restriction_active:
            continue
        if _matches(rule, triggered, payer_risk, recipient_risk, context):
            return rule
    raise RuntimeError("a DECISION_TABLE precisa terminar numa linha sem condição, como o D-OK")


def _matches(
    rule: DecisionRule, triggered: Collection[str], payer_risk: int, recipient_risk: int,
    context: Context,
) -> bool:
    if any(code not in triggered for code in rule.all_triggered):
        return False
    if rule.min_payer_risk is not None and payer_risk < rule.min_payer_risk:
        return False
    if rule.min_recipient_risk is not None and recipient_risk < rule.min_recipient_risk:
        return False
    if rule.payer_external == "adverse" and not (
        context.payer_external_adverse or context.payer_external_confirmed
    ):
        return False
    if rule.payer_external == "confirmed" and not context.payer_external_confirmed:
        return False
    if rule.recipient_external == "adverse" and not (
        context.recipient_external_adverse or context.recipient_external_confirmed
    ):
        return False
    if rule.recipient_external == "confirmed" and not context.recipient_external_confirmed:
        return False
    return True


# Repare que as frases leem a evidência pelo nome da chave: renomear uma
# chave numa camada quebra o motivo aqui, com KeyError.
def _reason(rule: DecisionRule, triggered: Mapping[str, Signal], payer_risk: int, recipient_risk: int) -> str:
    """A frase da linha da tabela que decidiu, com números da evidência.

    É uma frase só: os sinais, um a um, já estão em metadata.signals.
    """
    if rule.name == "D-RESTRICT-SIM":
        return "Restrição ativa para a transação"
    if rule.name in {"D-REJ-ATO", "D-REJ-PAY", "D-REJ-RCP", "D-REJ-CROSS"}:
        if rule.name == "D-REJ-RCP":
            return _risk_reason("do destino", Axis.RECIPIENT, recipient_risk, rule.min_recipient_risk, triggered)
        if rule.name == "D-REJ-CROSS":
            return (
                f"Risco cruzado (pagador {payer_risk}, destino {recipient_risk}) com evidência DICT adversa"
            )
        return _risk_reason("do pagador", Axis.PAYER, payer_risk, rule.min_payer_risk, triggered)
    if rule.name == "D-ATO-1":
        ratio = triggered["P06"].evidence["ratio"]
        return f"Dispositivo sem Pix aprovado deste pagador, com valor {_number(ratio)}x a mediana"
    if rule.name == "D-ATO-2":
        evidence = triggered["P03"].evidence
        prior = evidence.get("payer_prior_tx")
        if prior is None:
            return "Dispositivo não cadastrado e IP sem Pix aprovado deste pagador"
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
