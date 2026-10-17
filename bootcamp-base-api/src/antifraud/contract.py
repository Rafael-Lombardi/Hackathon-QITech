"""O contrato do motor antifraude: o que entra, o que cada camada devolve
e o que sai.

Entra um Context — o Pix normalizado e o histórico já contado pelo
controller. Cada camada devolve uma lista de Signal. O engine junta tudo
numa Analysis. As peças se falam só por estes tipos, e nenhum deles sabe
o que é banco, HTTP, variável de ambiente ou relógio: a mesma entrada dá
sempre a mesma análise. O banco continua atrás dos repositories e o DICT
atrás do connector, como no resto do Base Service — e quem quiser ver o
motor decidir roda ele num terminal, sem Postgres de pé.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Protocol
from zoneinfo import ZoneInfo

# Dia e noite, pro motor, são os de São Paulo. O payload chega com o fuso
# de quem mandou e o banco guarda o instante em UTC — nenhum dos dois diz
# que horas eram para o cliente. A regra noturna (L01) converte pra cá
# antes de olhar a hora, e os repositories cortam o dia civil com este
# mesmo fuso (prior_days, device_day_amount).
LOCAL_TIMEZONE = ZoneInfo("America/Sao_Paulo")


class SignalStatus(StrEnum):
    """Os três estados de um sinal, com o mesmo texto de pix_signal_status.

    unavailable existe pra que falta de histórico não vire resposta. Se
    virasse not_triggered, o primeiro Pix de um cliente sairia com P03
    dizendo que o aparelho é conhecido e REL01 dizendo que o destino
    também — um atestado de bom comportamento pra quem nunca foi visto. Se
    virasse ponto, todo cliente novo pagaria 2FA por ser novo. O sinal
    fica fora da soma, e o GET mostra que ele não tinha como responder.
    """

    TRIGGERED = "triggered"
    NOT_TRIGGERED = "not_triggered"
    UNAVAILABLE = "unavailable"


class Axis(StrEnum):
    """De quem é o risco. payer olha o comportamento e o acesso do pagador
    (alguém tomou a conta?); recipient olha o destino, a rede dele e a
    relação entre os dois (pra onde vai o dinheiro?)."""

    PAYER = "payer"
    RECIPIENT = "recipient"


class PixStatus(StrEnum):
    """O veredicto, com o mesmo texto de pix_transaction_status."""

    APPROVED = "approved"
    REJECTED = "rejected"
    TWO_FACTOR_REQUIRED = "two_factor_required"


class Sufficiency(StrEnum):
    """Quanto histórico sustenta a nota de um eixo. Não pontua: existe pra
    ninguém ler a nota 0 de quem nunca foi visto como conta limpa."""

    UNAVAILABLE = "unavailable"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


# O que a regra viu e com que limite comparou. Vai pra rule_result_data e
# volta em metadata.signals, então só leva tipo que o JSON entende — e
# nunca documento.
EvidenceValue = int | float | bool | str
Evidence = dict[str, EvidenceValue]


@dataclass(frozen=True)
class Transaction:
    """O Pix atual, do jeito que a normalização do controller deixou.

    Os documentos ficam fora do repr: um log descuidado do Context não
    pode vazar CPF inteiro.
    """

    amount: int  # centavos
    transaction_date: datetime  # com fuso
    payer_document: str = field(repr=False)  # só dígitos
    recipient_document: str = field(repr=False)
    payer_account_age_days: int  # (transaction_date - opening_date).days; negativo vira 0
    recipient_account_age_days: int  # da CONTA indicada no Pix, não de todas as do titular
    device_id: str
    ip: str
    channel: str

    def __post_init__(self) -> None:
        # Sem fuso, o astimezone de local_datetime assumiria o fuso da
        # máquina — UTC no container — e a regra noturna erraria por 3
        # horas sem avisar ninguém. Melhor quebrar aqui, e alto.
        if self.transaction_date.utcoffset() is None:
            raise ValueError("transaction_date precisa ter fuso")

    @property
    def local_datetime(self) -> datetime:
        """A hora do Pix em São Paulo — derivada, nunca informada.

        Podia ser um campo que o controller preenche. Ficou derivada
        porque um campo a mais deixa o Context chegar com duas datas que
        discordam, e o erro que este projeto já cometeu uma vez (ler a
        hora do payload sem converter) voltaria por ali. Assim só existe
        um caminho até a hora local.
        """
        return self.transaction_date.astimezone(LOCAL_TIMEZONE)


@dataclass(frozen=True)
class PayerHistory:
    """O passado do pagador, pelo documento — todas as contas dele somadas.

    Anterior é o que já estava gravado com transaction_date até a do Pix
    atual. O Pix atual ainda não foi gravado — isso só acontece depois da
    decisão —, e a regra que precisa dele soma uma vez. A exceção são as
    contagens de distintos, que já chegam contando o atual; o comentário
    do campo avisa.

    As contagens de rajada pegam qualquer status, porque a rajada em si é
    o sinal: contar só approved deixaria invisível o invasor que insiste
    depois do primeiro 2FA.
    """

    prior_attempts: int
    prior_days: int  # dias distintos, em São Paulo
    # Conhecido é ter Pix approved com ele. Um 2FA que ninguém confirmou
    # não pode ensinar o motor que o aparelho é do cliente — senão a
    # segunda tentativa do invasor passa limpa.
    device_known: bool
    ip_known: bool
    attempts_5m: int
    amount_1h: int  # centavos
    approved_count: int
    approved_days: int
    approved_median_amount: float | None  # percentile_cont(0.5) dos approved; None sem nenhum
    new_destinations_10m: int  # já inclui o recebedor atual, se ele for inédito
    device_known_before_today: bool  # approved com este aparelho antes da 0h de hoje
    device_day_amount: int  # hoje, com este aparelho, qualquer status


@dataclass(frozen=True)
class DeviceHistory:
    """O passado do aparelho, de qualquer pagador."""

    prior_attempts: int
    distinct_payers_24h: int  # já conta o pagador atual


@dataclass(frozen=True)
class RecipientHistory:
    """O passado do recebedor, pelo documento: o laranja com três contas
    em três bancos é um recebedor só."""

    prior_attempts: int
    prior_days: int
    attempts_10m: int  # só os anteriores; o R02 soma o atual
    distinct_payers_1h: int  # já conta o pagador atual


@dataclass(frozen=True)
class PairHistory:
    """O que este pagador já mandou pra este recebedor, documento a documento."""

    prior_approved: int
    first_approved_days_ago: int | None  # None sem nenhum approved
    recent_anomaly: bool  # rejected ou two_factor_required no par nos últimos 7 dias


@dataclass(frozen=True)
class Context:
    """Tudo o que o motor sabe de um Pix.

    Quem monta é o controller: o histórico vem dos repositories e o trust
    factor, do connector do DICT. O motor só lê.
    """

    transaction: Transaction
    payer: PayerHistory
    device: DeviceHistory
    recipient: RecipientHistory
    pair: PairHistory
    trust_factor: int | None  # 0 a 100; None quando a consulta ao DICT falhou


@dataclass(frozen=True)
class Signal:
    """O resultado de uma regra — uma linha de pix_transaction_rule_result.

    points é o do catálogo quando triggered e 0 nos outros dois estados;
    evidence só tem conteúdo quando triggered. Quem garante são as três
    fábricas do policy.py: camada nenhuma monta Signal à mão.
    """

    code: str
    layer: int
    family: str
    axis: Axis
    cap_group: str
    status: SignalStatus
    points: int
    evidence: Evidence


class Layer(Protocol):
    """Uma camada do motor: lê o Context e devolve os sinais dela.

    Camada nenhuma importa outra nem lê o sinal de outra; todas importam
    só este arquivo e o policy.py. Quando uma regra precisa de uma
    condição que mora em outra camada — o R04 é conta destino nova (a
    condição do R01, camada 1) com muitos pagadores (R03, camada 2) —, ela
    recalcula a condição a partir do Context.

    Custa uma comparação repetida. A alternativa era passar os sinais de
    uma camada pra seguinte, e ela amarraria a ordem: desligar a camada 1
    mudaria o R04 em silêncio. Assim cada camada roda sozinha com um
    Context, e o único lugar que cruza sinais de camadas diferentes é a
    tabela de decisão, no engine.
    """

    def evaluate(self, context: Context) -> list[Signal]:
        ...


@dataclass(frozen=True)
class Analysis:
    """A análise de um Pix, pronta pra gravar e devolver.

    status vira o status_id; os sete campos seguintes são as sete colunas
    de decisão de pix_transaction; signals vira as linhas de
    pix_transaction_rule_result, na ordem do catálogo.
    """

    status: PixStatus
    decision_rule: str
    reasons: tuple[str, ...]
    payer_risk: int
    recipient_risk: int
    payer_sufficiency: Sufficiency
    recipient_sufficiency: Sufficiency
    policy_version: str
    signals: tuple[Signal, ...]
