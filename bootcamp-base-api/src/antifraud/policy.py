"""A política do motor: todo número que ele usa, num arquivo só.

Pesos, limiares, janelas e tetos são hipóteses do time (RFC v0.3), não
calibragem com dado real; os que vêm do Banco Central dizem de onde
vieram. A POLICY_VERSION vai gravada em cada análise. Mudou um número
aqui, muda a versão — senão o motivo gravado ontem passa a ser lido com
os pesos de hoje.
"""

from dataclasses import dataclass
from datetime import timedelta

from antifraud.contract import Axis, Evidence, PixStatus, Signal, SignalStatus

# RFC v0.3 + a camada 3 (DICT01). A tabela de decisão é a da RFC, menos
# o teto de 69 do D-RCP (o porquê está na tabela).
POLICY_VERSION = "v0.3.1"


# ────────────────────────────────────────────────────────────────────
# Janelas de histórico
# ────────────────────────────────────────────────────────────────────
# Nenhuma regra lê janela: quem lê é o repository, e elas moram aqui pra
# que a consulta e a regra falem do mesmo número. Repare que a janela
# também está no nome do campo (attempts_5m), na chave da evidência e no
# nome do sinal: mudar uma janela é mudar os quatro, e no D-REJ também a
# frase do motivo.
#
# Com t = transaction_date do Pix atual, anterior é transaction_date <=
# t (o Pix atual ainda não foi gravado), e nos últimos X é
# transaction_date > t - X AND transaction_date <= t. Ao lado de cada
# janela, o campo do Context que ela define.
PAYER_ATTEMPTS_WINDOW = timedelta(minutes=5)  # attempts_5m
PAYER_AMOUNT_WINDOW = timedelta(hours=1)  # amount_1h
NEW_DESTINATIONS_WINDOW = timedelta(minutes=10)  # new_destinations_10m
DEVICE_PAYERS_WINDOW = timedelta(hours=24)  # distinct_payers_24h
RECIPIENT_ATTEMPTS_WINDOW = timedelta(minutes=10)  # attempts_10m
RECIPIENT_PAYERS_WINDOW = timedelta(hours=1)  # distinct_payers_1h
PAIR_ANOMALY_WINDOW = timedelta(days=7)  # recent_anomaly


# ────────────────────────────────────────────────────────────────────
# Limiares
# ────────────────────────────────────────────────────────────────────
# Dinheiro em centavos, como no payload: 100000 é R$ 1.000.

# Conta recém-aberta: menos de 7 dias (P01, R01 e a metade conta nova
# do R04).
NEW_ACCOUNT_DAYS = 7

# L01. Referência: o limite noturno do Banco Central, R$ 1.000 entre 20h
# e 6h. Aqui é sinal fraco e não teto: o Pix noturno passa, com 8
# pontos.
NIGHT_START_HOUR = 20
NIGHT_END_HOUR = 6
NIGHT_AMOUNT_LIMIT = 100000

# L02. Referência: o limite do Banco Central pra dispositivo não
# cadastrado, R$ 200 por Pix e R$ 1.000 por dia.
NEW_DEVICE_PIX_LIMIT = 20000
NEW_DEVICE_DAY_LIMIT = 100000

# Com menos de 3 Pix anteriores, aparelho e IP sem Pix aprovado não
# dizem nada: pra quem acabou de chegar, quase todo aparelho é novo.
# L02, P03, P09 e P10 ficam unavailable.
ACCESS_MIN_PRIOR_ATTEMPTS = 3

PAYER_ATTEMPTS_THRESHOLD = 5  # P04: 5 ou mais em 5 min, com o atual
PAYER_AMOUNT_LIMIT = 800000  # P05: mais de R$ 8.000 em 1 h, com o atual

# P06. Mais de 5 vezes a mediana, e só a partir de R$ 1.000: 5 vezes a
# mediana de quem paga R$ 30 é troco, e não justifica fricção. A
# mediana só vale com lastro, 10 Pix approved espalhados por 7 dias.
MEDIAN_MULTIPLIER = 5
ATYPICAL_MIN_AMOUNT = 100000
MEDIAN_MIN_APPROVED = 10
MEDIAN_MIN_DAYS = 7

NEW_DESTINATIONS_THRESHOLD = 3  # P07
SHARED_DEVICE_THRESHOLD = 3  # P08
RECIPIENT_ATTEMPTS_THRESHOLD = 5  # R02, contando o atual
RECIPIENT_PAYERS_THRESHOLD = 4  # R03 e R04

# REL02: 3 Pix approved no par, o primeiro há 7 dias ou mais.
RECURRENT_PAIR_MIN_APPROVED = 3
RECURRENT_PAIR_MIN_DAYS = 7

LOW_TRUST_LIMIT = 30  # DICT01: trust factor abaixo disso


# ────────────────────────────────────────────────────────────────────
# Catálogo
# ────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class SignalSpec:
    """O que se sabe de um sinal antes de qualquer Pix."""

    name: str
    layer: int
    family: str
    axis: Axis
    granularity: str
    cap_group: str
    points: int


# A ordem deste dicionário é a ordem dos sinais na resposta e no banco.
# Família é a do desafio e sai na resposta; grupo de teto é interno e
# só existe pra soma (veja CAPS).
#
# granularity é a chave da consulta que alimentou o sinal, como a RFC
# v0.3 pede pra não misturar titular com conta: a conta (source_account,
# destination_account), o documento (payer, recipient), o aparelho
# (device), o par de documentos (pair) ou o próprio Pix (transaction).
# O R04 cruza a conta destino com o documento recebedor e registra os
# dois.
CATALOG: dict[str, SignalSpec] = {
    # Camada 1 — o próprio Pix, sem histórico
    "P01": SignalSpec(
        name="Conta pagadora recém-aberta",
        layer=1, family="Perfil", axis=Axis.PAYER,
        granularity="source_account",
        cap_group="payer_account", points=10,
    ),
    "L01": SignalSpec(
        name="Valor alto no período noturno",
        layer=1, family="Limite", axis=Axis.PAYER,
        granularity="transaction",
        cap_group="limit", points=8,
    ),
    "R01": SignalSpec(
        name="Conta destino recém-aberta",
        layer=1, family="Contraparte", axis=Axis.RECIPIENT,
        granularity="destination_account",
        cap_group="recipient_network", points=12,
    ),
    "REL03": SignalSpec(
        name="Mesmo titular",
        layer=1, family="Contraparte", axis=Axis.RECIPIENT,
        granularity="pair",
        cap_group="relation", points=-5,
    ),
    # Camada 2 — o Pix contra o histórico
    "L02": SignalSpec(
        name="Valor acima do limite para dispositivo novo",
        layer=2, family="Limite", axis=Axis.PAYER,
        granularity="payer",
        cap_group="limit", points=10,
    ),
    "P03": SignalSpec(
        name="Dispositivo sem Pix aprovado do pagador",
        layer=2, family="Perfil", axis=Axis.PAYER,
        granularity="payer",
        cap_group="payer_behavior", points=18,
    ),
    "P04": SignalSpec(
        name="Muitas tentativas em 5 min",
        layer=2, family="Velocity", axis=Axis.PAYER,
        granularity="payer",
        cap_group="payer_velocity", points=20,
    ),
    "P05": SignalSpec(
        name="Montante alto na última hora",
        layer=2, family="Montante", axis=Axis.PAYER,
        granularity="payer",
        cap_group="payer_velocity", points=15,
    ),
    "P06": SignalSpec(
        name="Valor muito acima do padrão",
        layer=2, family="Perfil", axis=Axis.PAYER,
        granularity="payer",
        cap_group="payer_behavior", points=18,
    ),
    "P07": SignalSpec(
        name="Vários destinos inéditos em 10 min",
        layer=2, family="Velocity", axis=Axis.PAYER,
        granularity="payer",
        cap_group="payer_behavior", points=18,
    ),
    "P08": SignalSpec(
        name="Dispositivo usado por vários pagadores",
        layer=2, family="Perfil", axis=Axis.PAYER,
        granularity="device",
        cap_group="payer_behavior", points=12,
    ),
    "P09": SignalSpec(
        name="IP sem Pix aprovado do pagador",
        layer=2, family="Perfil", axis=Axis.PAYER,
        granularity="payer",
        cap_group="payer_behavior", points=6,
    ),
    "P10": SignalSpec(
        name="Dispositivo e IP sem Pix aprovado do pagador",
        layer=2, family="Perfil", axis=Axis.PAYER,
        granularity="payer",
        cap_group="payer_behavior", points=12,
    ),
    "R02": SignalSpec(
        name="Muitas entradas no mesmo destino",
        layer=2, family="Velocity", axis=Axis.RECIPIENT,
        granularity="recipient",
        cap_group="recipient_network", points=16,
    ),
    "R03": SignalSpec(
        name="Muitos pagadores para o mesmo destino",
        layer=2, family="Contraparte", axis=Axis.RECIPIENT,
        granularity="recipient",
        cap_group="recipient_network", points=20,
    ),
    "R04": SignalSpec(
        name="Conta nova concentrando pagadores",
        layer=2, family="Contraparte", axis=Axis.RECIPIENT,
        granularity="destination_account+recipient",
        cap_group="recipient_network", points=15,
    ),
    "REL01": SignalSpec(
        name="Destino inédito para o pagador",
        layer=2, family="Contraparte", axis=Axis.RECIPIENT,
        granularity="pair",
        cap_group="relation", points=8,
    ),
    "REL02": SignalSpec(
        name="Relação recorrente",
        layer=2, family="Contraparte", axis=Axis.RECIPIENT,
        granularity="pair",
        cap_group="relation", points=-8,
    ),
    # Camada 3 — o DICT.
    #
    # DICT01 responde à mesma pergunta do R01 e do R03 (o destino é
    # confiável?), e conta nova costuma ter nota baixa: em grupos
    # separados, a conta nova contaria duas vezes. Por isso ele divide o
    # teto de 35 do recipient_network, e o máximo do destino fica nos 43
    # da RFC. Com 32 e limiar 30, sozinho ele não pede 2FA (o DICT real
    # atualiza com até 12 h de atraso), e somado à conta nova para nos
    # 35. O preço: na prática o DICT01 só decide junto com o REL01, que
    # leva o destino aos 40 do D-RCP, e nunca pede 2FA no Pix de um
    # pagador sem histórico, em que o REL01 fica unavailable.
    "DICT01": SignalSpec(
        name="Destino com baixa confiança no DICT",
        layer=3, family="Contraparte", axis=Axis.RECIPIENT,
        granularity="recipient",
        cap_group="recipient_network", points=32,
    ),
}


def signal_spec(code: str) -> SignalSpec:
    """O que o catálogo diz de um sinal, pelo código.

    O banco grava de cada sinal o código, o eixo, a família, o status, os
    pontos e a evidência. Camada, granularidade e nome não são coluna: a
    resposta do GET busca aqui.
    """
    return CATALOG[code]


# ────────────────────────────────────────────────────────────────────
# Tetos e notas
# ────────────────────────────────────────────────────────────────────
# Teto é o máximo que um grupo de sinais parecidos soma, pra que cinco
# regras vendo a mesma rajada não contem cinco vezes. P04 e P05 olham o
# mesmo ataque por dois lados (quantas vezes, quanto dinheiro): juntos
# dariam 35, e o grupo para em 25. O preço é que o segundo indício de
# um grupo vale menos que o primeiro.
#
# relation é o único que desce: histórico bom desconta até 10 pontos, e
# nunca mais que isso — relação antiga não apaga sinal grave.
CAPS: dict[str, tuple[int, int]] = {
    "limit": (0, 15),
    "payer_account": (0, 10),
    "payer_velocity": (0, 25),
    "payer_behavior": (0, 45),
    "recipient_network": (0, 35),
    "relation": (-10, 8),
}

# A nota de um eixo é a soma dos grupos dele, presa entre 0 e 100. Com
# os tetos acima o pagador chega no máximo a 95, e o destino a 43. Não é
# probabilidade: 70 pontos não são 70% de chance de fraude.
MIN_RISK = 0
MAX_RISK = 100


# ────────────────────────────────────────────────────────────────────
# Suficiência
# ────────────────────────────────────────────────────────────────────
# Pelos Pix anteriores (n) e pelos dias distintos entre eles (d), de
# cada eixo: n = 0 é unavailable, high pede n >= 30 em d >= 7, medium
# pede n >= 6 em d >= 2, e o resto é low.
HIGH_SUFFICIENCY_MIN_ATTEMPTS = 30
HIGH_SUFFICIENCY_MIN_DAYS = 7
MEDIUM_SUFFICIENCY_MIN_ATTEMPTS = 6
MEDIUM_SUFFICIENCY_MIN_DAYS = 2


# ────────────────────────────────────────────────────────────────────
# Tabela de decisão
# ────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class DecisionRule:
    """Uma linha da tabela de decisão.

    Casa quando todos os sinais de all_triggered dispararam e as notas
    mínimas, se houver, foram atingidas. Linha sem condição nenhuma casa
    sempre.
    """

    name: str
    status: PixStatus
    all_triggered: tuple[str, ...] = ()
    min_payer_risk: int | None = None
    min_recipient_risk: int | None = None


# Em ordem, e vale a primeira que casar. Rejeição só por combinação no
# destino (D-REJ): o 2FA ajuda quando outra pessoa pode estar usando a
# conta, e não impede ninguém de mandar dinheiro pra um laranja. Conta
# nova, horário ou valor sozinhos nunca rejeitam. ATO é account
# takeover, a tomada de conta: aparelho sem Pix aprovado do pagador com
# valor fora do padrão (D-ATO-1), ou com IP também sem Pix aprovado
# (D-ATO-2).
#
# D-RCP é >= 40, sem o teto de 69 da RFC. O 69 vinha de quando 70 ou
# mais era rejeição, regra que saiu porque nunca era alcançada: com os
# tetos o destino chega no máximo a 43, e hoje o comportamento é o
# mesmo. A diferença aparece se os pesos mudarem: com o teto, um
# destino acima de 69 cairia no D-OK.
DECISION_TABLE: tuple[DecisionRule, ...] = (
    DecisionRule("D-REJ", PixStatus.REJECTED, all_triggered=("R01", "R02", "R03")),
    DecisionRule("D-ATO-1", PixStatus.TWO_FACTOR_REQUIRED, all_triggered=("P03", "P06")),
    DecisionRule("D-ATO-2", PixStatus.TWO_FACTOR_REQUIRED, all_triggered=("P03", "P09")),
    DecisionRule("D-PAY", PixStatus.TWO_FACTOR_REQUIRED, min_payer_risk=40),
    DecisionRule("D-RCP", PixStatus.TWO_FACTOR_REQUIRED, min_recipient_risk=40),
    DecisionRule("D-OK", PixStatus.APPROVED),
)


# ────────────────────────────────────────────────────────────────────
# Do catálogo ao Signal
# ────────────────────────────────────────────────────────────────────
# As camadas montam sinais só por estas três funções, e é aqui que mora
# a regra de que ponto e evidência só existem quando o sinal dispara.
#
# Evidência é o valor observado e o limite com que ele foi comparado.
# Com um limite só, a chave do limite é threshold (P04: attempts_5m e
# threshold); com mais de um, cada limite leva o nome do valor que
# limita (L02: amount e amount_threshold).
def triggered(code: str, evidence: Evidence) -> Signal:
    return _signal(code, SignalStatus.TRIGGERED, signal_spec(code).points, evidence)


def not_triggered(code: str) -> Signal:
    return _signal(code, SignalStatus.NOT_TRIGGERED, 0, {})


def unavailable(code: str) -> Signal:
    return _signal(code, SignalStatus.UNAVAILABLE, 0, {})


def _signal(code: str, status: SignalStatus, points: int, evidence: Evidence) -> Signal:
    spec = signal_spec(code)
    return Signal(
        code=code,
        layer=spec.layer,
        family=spec.family,
        axis=spec.axis,
        granularity=spec.granularity,
        cap_group=spec.cap_group,
        status=status,
        points=points,
        evidence=evidence,
    )
