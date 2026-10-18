"""A política do motor: todo número que ele usa, num arquivo só.

Pesos, limiares, janelas e tetos são hipóteses do time (a RFC v0.5, mais
as emendas listadas abaixo), não calibragem com dado real; os que vêm do
Banco Central dizem de onde vieram. A POLICY_VERSION vai
gravada em cada análise. Mudou um número aqui, muda a versão — senão o
motivo gravado ontem passa a ser lido com os pesos de hoje.
"""

from dataclasses import dataclass
from datetime import timedelta

from antifraud.contract import Axis, Evidence, PixStatus, Signal, SignalStatus

# v0.6.0 é a RFC v0.5 mais quatro emendas propostas para a v0.6, cada
# uma marcada como emenda onde mora (catálogo e tabela): o L03, teto por
# Pix, e três linhas de 2FA por combinação, D-LIMIT, D-SCAM e D-DRAIN.
# Nenhuma emenda mexe no que a RFC manda; cada uma pede confirmação num
# caso em que a v0.5 aprovaria. Onde ainda difere da RFC: o teto da rede
# do destino está em 40, e não em 35; a camada 3 lê a marcação de fraude
# do DICT, e não ocorrências datadas; e não há a rejeição do pagador por
# evidência do DICT (D-REJ-PAY).
POLICY_VERSION = "v0.6.0"


# ────────────────────────────────────────────────────────────────────
# Janelas de histórico
# ────────────────────────────────────────────────────────────────────
# Nenhuma regra lê janela: quem lê é o repository, e elas moram aqui pra
# que a consulta e a regra falem do mesmo número. Repare que a janela
# também está no nome do campo (attempts_5m), na chave da evidência e no
# nome do sinal: mudar uma janela é mudar os quatro, e no D-DRAIN também
# a frase do motivo.
#
# O relógio é o do servidor (RFC §2): com t = evaluated_at do Pix atual,
# lido depois da trava do pagador, anterior é received_at <= t (o Pix
# atual ainda não foi gravado, e um Pix recebido no mesmo instante conta
# — é o que deixa dois Pix do relógio de teste se enxergarem em corrida),
# e nos últimos X é received_at >= t - X, a janela fechada no começo como
# a RFC escreve. Ao lado de cada janela, o campo do Context que ela
# define.
PAYER_ATTEMPTS_WINDOW = timedelta(minutes=5)  # attempts_5m
PAYER_AMOUNT_WINDOW = timedelta(hours=1)  # amount_1h
NEW_DESTINATIONS_WINDOW = timedelta(minutes=10)  # new_destinations_10m
DEVICE_PAYERS_WINDOW = timedelta(hours=24)  # distinct_payers_24h
RECIPIENT_ATTEMPTS_WINDOW = timedelta(minutes=10)  # attempts_10m
RECIPIENT_PAYERS_WINDOW = timedelta(hours=1)  # distinct_payers_1h
PAIR_ANOMALY_WINDOW = timedelta(days=7)  # recent_anomaly


# ────────────────────────────────────────────────────────────────────
# Tolerância da data do Pix
# ────────────────────────────────────────────────────────────────────
# transaction_date é declarada por quem manda, e as janelas contam pelo
# relógio do servidor. Longe demais dele, a data não descreve um Pix de
# agora: 90 dias pra trás (a janela mais longa que alguma regra olha) e
# 5 minutos pra frente (relógio desacertado, e nada além disso). Fora
# disso é erro contratual, 400, e não veredito (RFC §2 e §5). Quem
# confere é o controller, antes do recibo.
TRANSACTION_DATE_MAX_AGE = timedelta(days=90)
TRANSACTION_DATE_MAX_SKEW = timedelta(minutes=5)


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
# cadastrado, R$ 200 por Pix e R$ 1.000 por dia. O dia é por cliente,
# somando todos os aparelhos sem cadastro dele, e não por aparelho.
NEW_DEVICE_PIX_LIMIT = 20000
NEW_DEVICE_DAY_LIMIT = 100000

# L03. "Passou do teto?" é a primeira pergunta do desafio, e todo banco
# aplica um teto por Pix. R$ 50.000 é o nosso; quem nunca teve Pix
# aprovado nesta base tem R$ 5.000, como a conta recém-aberta tem limite
# inicial baixo até mostrar a que veio. O teto conta a partir do próprio
# valor (>=): é o primeiro valor que não passa sem confirmação. Pra si
# mesmo não há teto: o L03 só olha Pix pra terceiro.
PIX_AMOUNT_CAP = 5_000_000
COLD_START_AMOUNT_CAP = 500_000

# P09 compara o IP com os dos Pix aprovados, e fica unavailable (e com
# ele o P10) enquanto o pagador não tiver 3 Pix aprovados: antes disso
# não há IP conhecido a perder. A conta é de aprovados, e não de
# tentativas, porque o 2FA não volta pra esta API: contando tentativas,
# o cliente novo cujos 3 primeiros Pix não foram aprovados teria, no 4º,
# um IP "nunca visto" de graça. O aparelho não passa por aqui — o P03
# pergunta ao cadastro do banco, que responde desde o primeiro Pix.
ACCESS_MIN_APPROVED = 3

PAYER_ATTEMPTS_THRESHOLD = 5  # P04: 5 ou mais em 5 min, com o atual
PAYER_AMOUNT_LIMIT = 800000  # P05: mais de R$ 8.000 em 1 h, com o atual

# P06. Mais de 5 vezes a mediana, e só a partir de R$ 1.000: 5 vezes a
# mediana de quem paga R$ 30 é troco, e não justifica fricção. A
# mediana só vale com lastro, 10 Pix aprovados espalhados por 7 dias.
MEDIAN_MULTIPLIER = 5
ATYPICAL_MIN_AMOUNT = 100000
MEDIAN_MIN_APPROVED = 10
MEDIAN_MIN_DAYS = 7

NEW_DESTINATIONS_THRESHOLD = 3  # P07
SHARED_DEVICE_THRESHOLD = 3  # P08
RECIPIENT_ATTEMPTS_THRESHOLD = 5  # R02, contando o atual
RECIPIENT_PAYERS_THRESHOLD = 4  # R03 e R04

# REL02: 3 Pix aprovados no par, o primeiro há 7 dias ou mais.
RECURRENT_PAIR_MIN_APPROVED = 3
RECURRENT_PAIR_MIN_DAYS = 7

LOW_SECURITY_STATISTICS_LIMIT = 30  # DICT01: estatística de segurança abaixo disso

# G01. Longe é mais de 300 km de todo centro habitual do pagador. Centro
# habitual: os Pix aprovados com localização dos últimos 90 dias, até 24 h
# antes deste (o Pix atual não ensina a si mesmo, e a rajada de hoje não
# vira "casa"), agrupados num raio de 100 km; só conta o centro visto em
# 2 dias distintos. Sem 10 observações em 7 dias não há padrão, e o sinal
# fica unavailable. Viagem é comum e GPS erra: 6 pontos, e sozinho passa.
LOCATION_DISTANCE_THRESHOLD_KM = 300
LOCATION_MIN_OBSERVATIONS = 10
LOCATION_MIN_DAYS = 7
LOCATION_MIN_DAYS_PER_CENTER = 2
LOCATION_CLUSTER_RADIUS_KM = 100
LOCATION_HISTORY_WINDOW = timedelta(days=90)
LOCATION_RECENCY_GAP = timedelta(hours=24)


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
# pede pra não misturar titular com conta: a conta (source_account,
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
        name="Valor acima do limite para dispositivo não cadastrado",
        layer=2, family="Limite", axis=Axis.PAYER,
        granularity="payer",
        cap_group="limit", points=10,
    ),
    # Emenda proposta para a v0.6 (fora da RFC v0.5): o teto por Pix.
    "L03": SignalSpec(
        name="Valor acima do teto por Pix",
        layer=2, family="Limite", axis=Axis.PAYER,
        granularity="payer",
        cap_group="amount_cap", points=15,
    ),
    "G01": SignalSpec(
        name="Localização distante do padrão",
        layer=2, family="Localização", axis=Axis.PAYER,
        granularity="payer", cap_group="payer_behavior", points=6,
    ),
    "P03": SignalSpec(
        name="Dispositivo não cadastrado no banco",
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
        name="Dispositivo não cadastrado e IP sem Pix aprovado do pagador",
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
    # O DICT01 soma por fora do teto da rede: a nota do DICT diz o que o
    # sistema Pix inteiro registrou do recebedor, coisa que o nosso
    # histórico não vê. Sozinho, o 32 fica abaixo dos 40 de propósito,
    # porque o DICT real atualiza com até 12 h de atraso. Com qualquer
    # outro indício do destino, pede 2FA: R01 dá 44, REL01 dá 40, e R02
    # ou R03 pegam também a conta já aquecida, de 7 dias ou mais, que
    # recebe rajada com nota baixa.
    #
    # Contar a conta nova duas vezes, no R01 e na nota, é de propósito:
    # conta recém-aberta com nota ruim é o retrato do laranja. Dividindo
    # o teto da rede com R01 a R04, a nota do DICT sumiria justo na conta
    # que já encosta nele, e a rajada na conta mal avaliada não somaria
    # nada. O preço é a conta recém-aberta honesta, com nota baixa só por
    # ser nova: ela pede 2FA de todo pagador, menos o próprio titular
    # (REL03) e quem já tem relação recorrente com ele (REL02).
    "DICT01": SignalSpec(
        name="Estatística de segurança DICT desfavorável",
        layer=3, family="Contraparte", axis=Axis.RECIPIENT,
        granularity="recipient",
        cap_group="dict_security_statistics", points=32,
    ),
    "DICT02": SignalSpec(
        name="Marcação de fraude suspeita no DICT",
        layer=3, family="Contraparte", axis=Axis.RECIPIENT,
        granularity="recipient", cap_group="dict_security", points=20,
    ),
    "DICT03": SignalSpec(
        name="Marcação de fraude confirmada no DICT",
        layer=3, family="Contraparte", axis=Axis.RECIPIENT,
        granularity="recipient", cap_group="dict_security", points=25,
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
#
# payer_account, amount_cap e dict_security_statistics têm um sinal só, e
# o teto é o próprio peso: não dividem teto com ninguém. dict_security tem
# dois que nunca disparam juntos (a marcação é suspeita OU confirmada), e
# o teto é o peso do DICT03. Mudou o peso, muda o teto.
#
# recipient_network em 40: a conta nova concentrando pagadores (R01 +
# R02 + R03 + R04 = 63, ou 47 sem a rajada do R02) chega ao teto e pede
# 2FA sozinha, pelo D-RCP; a loja madura em rajada (R02 + R03 = 36)
# continua passando. Rejeitar de vez fica pra quando o DICT marcou o
# destino.
CAPS: dict[str, tuple[int, int]] = {
    "limit": (0, 15),
    "amount_cap": (0, 15),
    "payer_account": (0, 10),
    "payer_velocity": (0, 25),
    "payer_behavior": (0, 45),
    "recipient_network": (0, 40),
    "dict_security_statistics": (0, 32),
    "dict_security": (0, 25),
    "relation": (-10, 8),
}

# A nota de um eixo é a soma dos grupos dele, presa entre 0 e 100. Com
# os tetos acima o pagador somaria até 110 e o destino até 105, e os
# dois param em 100. Não é probabilidade: 70 pontos não são 70% de
# chance de fraude.
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

    Casa quando todos os sinais de all_triggered dispararam, nenhum dos
    de none_triggered disparou, as notas mínimas, se houver, foram
    atingidas e, se a linha pede, há restrição ativa contra alguém do
    Pix, o DICT tem marcação de fraude contra o recebedor (suspeita ou
    confirmada) ou o cadastro de aparelhos está fora do ar. Linha sem
    condição nenhuma casa sempre.
    """

    name: str
    status: PixStatus
    all_triggered: tuple[str, ...] = ()
    none_triggered: tuple[str, ...] = ()
    min_payer_risk: int | None = None
    min_recipient_risk: int | None = None
    requires_restriction: bool = False
    requires_recipient_adverse: bool = False
    requires_device_registry_unavailable: bool = False


# Em ordem, vale a primeira linha que casar. A restrição vem na frente de
# tudo: é o banco dizendo que esta pessoa, ou esta conta, está bloqueada,
# e nota nenhuma discute isso. Depois as rejeições, antes de qualquer
# 2FA, e duas delas pedem a marcação de fraude do recebedor no DICT: a
# marcação é a porteira da rejeição, e não ponto — os pontos dela já
# estão no DICT02 e no DICT03. Sem marcação, nota alta continua dando
# 2FA. É o lado que segura o falso positivo: rejeitar de vez fica pra
# quem o sistema Pix inteiro já marcou, ou pra tomada de conta com todos
# os sinais juntos.
DECISION_TABLE: tuple[DecisionRule, ...] = (
    DecisionRule("D-RESTRICT-SIM", PixStatus.REJECTED, requires_restriction=True),
    # Aparelho sem cadastro, rajada, valor atípico e destinos inéditos ao
    # mesmo tempo, com o pagador em 70 ou mais: é o dreno da conta tomada,
    # e a única rejeição que não precisa do DICT.
    DecisionRule(
        "D-REJ-ATO", PixStatus.REJECTED,
        all_triggered=("P03", "P04", "P06", "P07"), min_payer_risk=70,
    ),
    # Destino marcado no DICT recebendo rajada de muita gente.
    DecisionRule(
        "D-REJ-RCP", PixStatus.REJECTED,
        all_triggered=("R02", "R03"), min_recipient_risk=55,
        requires_recipient_adverse=True,
    ),
    # Destino marcado no DICT com os dois eixos já no limiar do 2FA.
    DecisionRule(
        "D-REJ-CROSS", PixStatus.REJECTED,
        min_payer_risk=40, min_recipient_risk=40,
        requires_recipient_adverse=True,
    ),
    DecisionRule("D-ATO-1", PixStatus.TWO_FACTOR_REQUIRED, all_triggered=("P03", "P06")),
    DecisionRule("D-ATO-2", PixStatus.TWO_FACTOR_REQUIRED, all_triggered=("P03", "P09")),
    DecisionRule("D-PAY", PixStatus.TWO_FACTOR_REQUIRED, min_payer_risk=40),
    # As três linhas seguintes são emendas propostas para a v0.6, fora da
    # RFC v0.5: 2FA por combinação de sinais, em casos que a v0.5
    # aprovaria. Passou do teto por Pix: confirmação, seja qual for a
    # nota.
    DecisionRule("D-LIMIT", PixStatus.TWO_FACTOR_REQUIRED, all_triggered=("L03",)),
    # Valor atípico pra um destino que o pagador nunca pagou, do próprio
    # aparelho: a falsa central, o falso parente, o sequestro relâmpago —
    # os golpes em que a vítima mesma manda. A autotransferência (REL03)
    # fica de fora: pra si mesmo, destino inédito não é inédito.
    DecisionRule(
        "D-SCAM", PixStatus.TWO_FACTOR_REQUIRED,
        all_triggered=("P06", "REL01"), none_triggered=("REL03",),
    ),
    # Rajada pra destinos inéditos: o dreno da conta tomada em parcelas
    # pequenas, antes de a nota chegar aos 40.
    DecisionRule("D-DRAIN", PixStatus.TWO_FACTOR_REQUIRED, all_triggered=("P04", "P07")),
    DecisionRule("D-RCP", PixStatus.TWO_FACTOR_REQUIRED, min_recipient_risk=40),
    # Só quando nada acima casou: o cadastro de aparelhos não respondeu,
    # e aprovar seria aprovar sem saber de que aparelho o Pix saiu. É
    # fallback, e não rejeição — fonte fora do ar não é indício contra
    # ninguém, e um Pix que já caiu numa linha de cima fica onde caiu
    # (RFC §4 e §7).
    DecisionRule("D-DATA-UNAVAILABLE", PixStatus.TWO_FACTOR_REQUIRED, requires_device_registry_unavailable=True),
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
