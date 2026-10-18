"""O contrato do motor antifraude: os tipos que as peças trocam entre si.

Entra um Context — o Pix normalizado, o histórico já contado e o que as
fontes de fora responderam, tudo montado pelo controller. Cada camada
devolve uma lista de Signal, e o engine junta tudo numa Analysis. Nenhum desses tipos sabe o que é banco, HTTP,
variável de ambiente ou relógio: a mesma entrada dá sempre a mesma
análise.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Protocol
from zoneinfo import ZoneInfo

# Dia e noite, pro motor, são os de São Paulo. O payload chega com o
# fuso de quem mandou e o banco guarda o instante em UTC — nenhum dos
# dois diz que horas eram para o cliente. A regra noturna (L01) converte
# pra cá antes de olhar a hora, e os repositories cortam o dia civil com
# este mesmo fuso (prior_days, unregistered_device_day_amount).
LOCAL_TIMEZONE = ZoneInfo("America/Sao_Paulo")


class SignalStatus(StrEnum):
    """Os três estados de um sinal, com o texto de pix_signal_status.

    unavailable existe pra que falta de histórico não vire resposta. Se
    virasse not_triggered, o primeiro Pix de um cliente sairia com P03
    dizendo que o aparelho é conhecido e REL01 dizendo que o destino
    também — um atestado de bom comportamento pra quem nunca foi visto.
    Se virasse ponto, todo cliente novo pagaria 2FA por ser novo. O sinal
    fica fora da soma, e o GET mostra que ele não tinha como responder.
    """

    TRIGGERED = "triggered"
    NOT_TRIGGERED = "not_triggered"
    UNAVAILABLE = "unavailable"


class Axis(StrEnum):
    """O eixo do risco, ou de quem é a suspeita.

    payer olha o comportamento e o acesso do pagador (alguém tomou a
    conta?); recipient olha o destino, a rede dele e a relação entre os
    dois (pra onde vai o dinheiro?).
    """

    PAYER = "payer"
    RECIPIENT = "recipient"


class PixStatus(StrEnum):
    """O veredicto, com o mesmo texto de pix_transaction_status."""

    APPROVED = "approved"
    REJECTED = "rejected"
    TWO_FACTOR_REQUIRED = "two_factor_required"


class Sufficiency(StrEnum):
    """Quanto histórico sustenta a nota de um eixo.

    Não pontua: existe pra ninguém ler a nota 0 de quem nunca foi visto
    como conta limpa.
    """

    UNAVAILABLE = "unavailable"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class FraudMarker(StrEnum):
    """A marcação de fraude de um documento no DICT.

    Além das estatísticas, o DICT guarda as marcações de fraude que os
    participantes registram contra uma chave ou um documento (Manual
    Operacional do DICT, cap. 10 e 18): a suspeita, aberta por quem
    notificou, e a confirmada. NONE é o DICT dizendo que não há marcação.
    DICT fora do ar não é marcação nenhuma: aí o Context carrega None, e
    o DICT02 e o DICT03 saem unavailable.
    """

    NONE = "none"
    SUSPICIOUS = "suspicious"
    CONFIRMED = "confirmed"


class RegistrationStatus(StrEnum):
    """O que o cadastro de aparelhos do banco diz do par pagador–aparelho.

    Os dois primeiros são respostas: o cadastro conhece o pagador, e o
    aparelho está cadastrado e vigente neste instante, ou não está. Os
    dois últimos são falta de resposta, e a RFC manda não confundir
    nenhum deles com unregistered: uncovered é o pagador fora da fixture
    do cadastro (dado parcial, e ausência em dado parcial não é
    não-cadastro); unavailable é a fonte que não respondeu. Nos dois o
    P03 sai unavailable; só o unavailable troca o D-OK pelo
    D-DATA-UNAVAILABLE, porque aí a fonte que devia responder não
    respondeu.
    """

    REGISTERED = "registered"
    UNREGISTERED = "unregistered"
    UNCOVERED = "uncovered"
    UNAVAILABLE = "unavailable"


class RestrictionScope(StrEnum):
    """De quem é uma restrição: do pagador, do titular do destino ou da conta exata de destino."""

    PAYER = "payer"
    RECIPIENT_HOLDER = "recipient_holder"
    RECIPIENT_ACCOUNT = "recipient_account"


# O que a regra viu e com que limite comparou. Vai pra rule_result_data
# e volta em metadata.signals, então só leva tipo que o JSON entende — e
# nunca documento.
EvidenceValue = int | float | bool | str
Evidence = dict[str, EvidenceValue]


@dataclass(frozen=True)
class DeviceRegistration:
    """O aparelho do Pix, pelo cadastro do banco e pelo histórico.

    status e source são o que o cadastro respondeu (source é a
    proveniência que ele declarou, demo_seed na fixture; None quando ele
    não respondeu ou não cobre o pagador). previously_observed vem do
    histórico, não do cadastro: o par já apareceu em alguma tentativa
    anterior, de qualquer status. São perguntas diferentes, e a RFC pede
    as duas separadas — tentativa não cadastra aparelho, e um 2FA que
    ninguém confirmou não pode virar familiaridade.
    """

    status: RegistrationStatus
    source: str | None
    previously_observed: bool


@dataclass(frozen=True)
class Restriction:
    """Uma restrição interna, como a fonte a descreve.

    scope diz de quem ela é e identity_key diz qual: o documento do
    pagador ou do titular, ou ispb:agência:conta da conta exata. A
    vigência tem três datas, e active_at junta as três do jeito que a RFC
    escreve (§7). identity_key fica fora do repr e da resposta: é
    documento.
    """

    restriction_id: str
    scope: RestrictionScope
    identity_key: str = field(repr=False)
    reason: str
    evidence_ref: str
    provenance: str
    effective_at: datetime
    expires_at: datetime | None
    revoked_at: datetime | None

    def active_at(self, moment: datetime) -> bool:
        """Vale em `moment`: já começou, não venceu e não foi revogada."""
        return (
            self.effective_at <= moment
            and (self.expires_at is None or moment < self.expires_at)
            and (self.revoked_at is None or moment < self.revoked_at)
        )

    def applies_to(self, *, payer_document: str, recipient_document: str, recipient_account_key: str) -> bool:
        """Fala de alguém desta transação: o pagador, o titular do destino ou a conta exata."""
        identities = {
            RestrictionScope.PAYER: payer_document,
            RestrictionScope.RECIPIENT_HOLDER: recipient_document,
            RestrictionScope.RECIPIENT_ACCOUNT: recipient_account_key,
        }
        return identities[self.scope] == self.identity_key


@dataclass(frozen=True)
class Transaction:
    """O Pix atual, do jeito que a normalização do controller deixou.

    Só leva o que alguma regra lê. Aparelho, IP e canal o controller
    grava direto, e pro motor eles chegam de outro jeito: o IP como
    histórico (ip_known), o aparelho como o cadastro do banco o vê
    (DeviceRegistration) e como o histórico o viu (DeviceHistory); a
    geolocalização idem, como a distância até os lugares de sempre
    (location_distance_km). Os documentos ficam fora do repr: um log
    descuidado do Context não pode vazar CPF inteiro.
    """

    amount: int  # centavos
    # A data do payload: o tempo do negócio, de onde saem a hora local do
    # L01 e a idade das contas. O relógio das janelas é outro, o do
    # servidor, e ele não chega ao motor: o histórico já vem contado.
    transaction_date: datetime  # com fuso
    payer_document: str = field(repr=False)  # só dígitos
    recipient_document: str = field(repr=False)
    # Idade em dias de 24 h: (transaction_date - opening_date).days, e
    # negativa vira 0. A do recebedor é da CONTA indicada no Pix, e não
    # de todas as contas do titular.
    payer_account_age_days: int
    recipient_account_age_days: int
    # Quilômetros até o centro habitual mais perto; None sem localização
    # no Pix ou sem histórico que forme um padrão (o G01 fica unavailable).
    location_distance_km: float | None = None

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
    """O passado do pagador, pelo documento: todas as contas dele somadas.

    Anterior é o que já estava gravado com received_at até o instante da
    avaliação deste Pix, inclusive. O Pix atual ainda não foi gravado
    (isso só acontece depois da decisão), e a regra que precisa dele soma
    o atual uma vez. A exceção são as contagens de distintos, que já
    chegam contando o atual; o comentário do campo avisa.

    As contagens de rajada pegam qualquer status, porque a rajada em si
    é o sinal: contar só os aprovados deixaria invisível o invasor que
    insiste depois do primeiro 2FA.

    Teto: prior_days, approved_count e approved_days podem chegar
    parados no limiar que os lê (HIGH_SUFFICIENCY_MIN_DAYS,
    MEDIAN_MIN_APPROVED e MEDIAN_MIN_DAYS, do policy.py). O motor só os
    compara com limiar, e contar além dele é consulta à toa; o teto é a
    própria constante, então mudou o limiar, muda o teto. Repare que o
    approved_count tem dois leitores, o lastro do P06 e a pré-condição
    do IP (ACCESS_MIN_APPROVED): o teto precisa ficar no maior dos dois
    limiares, hoje o MEDIAN_MIN_APPROVED (10). O resto precisa ser
    exato: prior_attempts vira payer_prior_tx na evidência e no motivo do
    D-ATO-2, e as janelas e new_destinations_10m saem na evidência do
    jeito que foram contados.
    """

    prior_attempts: int
    prior_days: int  # dias civis distintos, em São Paulo
    # Conhecido é já ter sido usado num Pix aprovado deste pagador, e não
    # só visto. Um 2FA que ninguém confirmou não pode ensinar o motor que
    # o IP é do cliente, senão a segunda tentativa do invasor passa
    # limpa. Na evidência, ip_seen_before (o nome do exemplo da RFC) quer
    # dizer isto.
    ip_known: bool
    # O par pagador–aparelho já apareceu em alguma tentativa anterior, de
    # qualquer status. Não diz que o aparelho é do cliente — isso é o
    # cadastro do banco, em DeviceRegistration —; vai pra resposta como
    # previously_observed, pra que "visto" e "cadastrado" nunca se
    # confundam.
    device_previously_observed: bool
    attempts_5m: int
    amount_1h: int  # centavos
    approved_count: int
    approved_days: int
    # percentile_cont(0.5) dos aprovados. None sem nenhum aprovado, ou com
    # o Pix abaixo do piso do P06 (ATYPICAL_MIN_AMOUNT): aí o P06 responde
    # sem ela, e o controller pode pular a consulta.
    approved_median_amount: float | None
    # Recebedores distintos sem Pix aprovado deste pagador antes da
    # janela, contando o recebedor atual.
    new_destinations_10m: int
    # A soma de hoje (dia civil de São Paulo) das tentativas do pagador,
    # de qualquer status, feitas de aparelho que NA HORA não estava
    # cadastrado — pelo status gravado em cada tentativa, e não pelo
    # cadastro de agora: cadastrar o aparelho à tarde não apaga o que
    # saiu dele de manhã. Em todos os aparelhos, e não só no atual: o
    # limite diário do Banco Central é por cliente, e dois celulares não
    # dividem o teto. O L02 soma o Pix atual por cima.
    unregistered_device_day_amount: int  # centavos


@dataclass(frozen=True)
class DeviceHistory:
    """O passado do aparelho, de qualquer pagador.

    prior_attempts pode ser um EXISTS, 0 ou 1: o P08 só pergunta se o
    aparelho já apareceu. distinct_payers_24h precisa ser exato, porque
    vai pra evidência.
    """

    prior_attempts: int
    distinct_payers_24h: int  # já conta o pagador atual


@dataclass(frozen=True)
class RecipientHistory:
    """O passado do recebedor, pelo documento.

    O laranja com três contas em três bancos é um recebedor só.

    Teto: prior_attempts e prior_days podem chegar parados em
    HIGH_SUFFICIENCY_MIN_ATTEMPTS e HIGH_SUFFICIENCY_MIN_DAYS, do
    policy.py. Quem lê os dois é só a suficiência e a pergunta de se o
    recebedor já apareceu. attempts_10m e distinct_payers_1h precisam ser
    exatos, porque vão pra evidência.
    """

    prior_attempts: int
    prior_days: int
    attempts_10m: int  # só os anteriores; o R02 soma o atual
    distinct_payers_1h: int  # já conta o pagador atual


@dataclass(frozen=True)
class PairHistory:
    """O que este pagador já mandou pra este recebedor, por documento.

    Tudo exato: os três campos vão pra evidência do REL01 e do REL02.
    """

    prior_approved: int
    # Dias de 24 h entre o primeiro aprovado do par e a avaliação deste
    # Pix, como a idade das contas: (evaluated_at - primeiro).days. None
    # sem nenhum aprovado.
    first_approved_days_ago: int | None
    # rejected ou two_factor_required no par, nos últimos 7 dias
    recent_anomaly: bool


@dataclass(frozen=True)
class Context:
    """Tudo o que o motor sabe de um Pix.

    Quem monta é o controller: o histórico vem dos repositories, o
    cadastro do aparelho e as restrições vêm dos connectors de cada
    fonte, e a estatística de segurança e a marcação de fraude do
    recebedor vêm do connector do DICT, numa consulta só. O motor só lê.

    Alguns valores o controller preenche sem consultar, porque saem
    exatos da própria definição. Pagador sem Pix anterior
    (payer.prior_attempts = 0) tem o resto do PayerHistory e o
    PairHistory inteiro zerados, False ou None, e
    new_destinations_10m = 1, que é o recebedor atual. Recebedor sem Pix
    anterior tem attempts_10m = 0 e distinct_payers_1h = 1, o pagador
    atual. Aparelho sem Pix anterior tem distinct_payers_24h = 1.
    """

    transaction: Transaction
    payer: PayerHistory
    device: DeviceHistory
    recipient: RecipientHistory
    pair: PairHistory
    device_registration: DeviceRegistration
    # Só as que valem neste instante e falam deste pagador, deste titular
    # de destino ou desta conta exata: o controller filtra, e qualquer
    # uma aqui rejeita (D-RESTRICT-SIM). Quase sempre vazia.
    restrictions: tuple[Restriction, ...]
    # Os dois vêm da mesma resposta do DICT sobre o recebedor, e os dois
    # ficam None quando o DICT falhou: a camada 3 inteira sai unavailable.
    dict_security_statistics: int | None
    dict_fraud_marker: FraudMarker | None


@dataclass(frozen=True)
class Signal:
    """O resultado de uma regra, uma linha de pix_transaction_rule_result.

    points é o do catálogo quando triggered e 0 nos outros dois estados;
    evidence só tem conteúdo quando triggered. Quem garante são as três
    fábricas do policy.py: nenhuma camada monta Signal à mão.
    """

    code: str
    layer: int
    family: str
    axis: Axis
    granularity: str
    cap_group: str
    status: SignalStatus
    points: int
    evidence: Evidence


class Layer(Protocol):
    """Uma camada do motor: lê o Context e devolve os sinais dela.

    Nenhuma camada importa outra nem lê o sinal de outra; todas importam
    só este arquivo e o policy.py. Quando uma regra precisa de uma
    condição que mora em outra camada, ela recalcula a condição a partir
    do Context: o R04 é conta destino nova (a condição do R01, camada 1)
    com muitos pagadores (a do R03, camada 2).

    Custa uma comparação repetida. A alternativa era passar os sinais de
    uma camada pra seguinte, e ela amarraria a ordem: desligar a camada
    1 mudaria o R04 em silêncio. Assim cada camada roda sozinha com um
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
