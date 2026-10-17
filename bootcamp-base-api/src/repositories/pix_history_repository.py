from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from database import Context
from models import PixTransactionStatus
from repositories.pix_transaction_repository import PixTransactionRepository


@dataclass(frozen=True)
class PayerFeatures:
    """O passado do pagador, pelo documento."""

    prior_attempts: int
    prior_days: int
    device_known: bool
    ip_known: bool
    attempts_5m: int
    amount_1h: int
    approved_count: int
    approved_days: int
    approved_median_amount: float | None
    new_destinations_10m: int
    device_known_before_today: bool
    device_day_amount: int


@dataclass(frozen=True)
class DeviceFeatures:
    """O passado do aparelho, de qualquer pagador."""

    prior_attempts: int
    distinct_payers_24h: int


@dataclass(frozen=True)
class RecipientFeatures:
    """O passado do recebedor, pelo documento."""

    prior_attempts: int
    prior_days: int
    attempts_10m: int
    distinct_payers_1h: int


@dataclass(frozen=True)
class PairFeatures:
    """O passado do par pagador→recebedor, documento a documento."""

    prior_approved: int
    first_approved_days_ago: int | None
    recent_anomaly: bool


# Os Pix do pagador viram uma CTE, lida uma vez, e cada feature é um
# agregado sobre ela. new_destinations é conjunto: os recebedores da
# janela, mais o atual, menos os que já tinham um Pix approved antes da
# janela. O UNION junta sem repetir, e o EXCEPT tira.
_PAYER_QUERY = text("""
    WITH payer_pix AS (
        SELECT pix.transaction_date,
               (pix.transaction_date AT TIME ZONE 'America/Sao_Paulo')::date AS local_day,
               pix.amount,
               pix.status_id,
               pix.device_id,
               pix.ip,
               recipient.customer_id AS recipient_customer_id
        FROM account payer
        JOIN pix_transaction pix ON pix.source_account_id = payer.id
        JOIN account recipient ON recipient.id = pix.destination_account_id
        WHERE payer.customer_id = :payer_customer_id
          AND pix.transaction_date <= :t
    ),
    new_destinations AS (
        SELECT recipient_customer_id
        FROM payer_pix
        WHERE transaction_date > :new_destinations_since
        UNION
        SELECT :recipient_customer_id
        EXCEPT
        SELECT recipient_customer_id
        FROM payer_pix
        WHERE status_id = :approved
          AND transaction_date <= :new_destinations_since
    )
    SELECT count(*) AS prior_attempts,
           count(DISTINCT local_day) AS prior_days,
           count(*) FILTER (WHERE status_id = :approved AND device_id = :device_id) > 0 AS device_known,
           count(*) FILTER (WHERE status_id = :approved AND ip = :ip) > 0 AS ip_known,
           count(*) FILTER (WHERE transaction_date > :attempts_since) AS attempts_5m,
           coalesce(sum(amount) FILTER (WHERE transaction_date > :amount_since), 0) AS amount_1h,
           count(*) FILTER (WHERE status_id = :approved) AS approved_count,
           count(DISTINCT local_day) FILTER (WHERE status_id = :approved) AS approved_days,
           percentile_cont(0.5) WITHIN GROUP (ORDER BY amount)
               FILTER (WHERE status_id = :approved) AS approved_median_amount,
           (SELECT count(*) FROM new_destinations) AS new_destinations_10m,
           count(*) FILTER (WHERE status_id = :approved
                              AND device_id = :device_id
                              AND local_day < (:t AT TIME ZONE 'America/Sao_Paulo')::date) > 0
               AS device_known_before_today,
           coalesce(sum(amount) FILTER (WHERE device_id = :device_id
                                          AND local_day = (:t AT TIME ZONE 'America/Sao_Paulo')::date), 0)
               AS device_day_amount
    FROM payer_pix
""")

# O total de Pix do aparelho só precisa das colunas do índice
# (device_id, transaction_date). O JOIN com account, pra chegar ao
# documento de cada pagador, fica restrito à janela.
_DEVICE_QUERY = text("""
    WITH payers_in_window AS (
        SELECT payer.customer_id
        FROM pix_transaction pix
        JOIN account payer ON payer.id = pix.source_account_id
        WHERE pix.device_id = :device_id
          AND pix.transaction_date > :payers_since
          AND pix.transaction_date <= :t
        UNION
        SELECT :payer_customer_id
    )
    SELECT count(*) AS prior_attempts,
           (SELECT count(*) FROM payers_in_window) AS distinct_payers_24h
    FROM pix_transaction
    WHERE device_id = :device_id
      AND transaction_date <= :t
""")

# Mesmo desenho do aparelho. Repare que a leitura do histórico inteiro só
# usa transaction_date, que está no índice (destination_account_id,
# transaction_date): um destino popular, com milhares de Pix, é contado
# sem ler a tabela. O JOIN pros pagadores fica restrito à janela.
_RECIPIENT_QUERY = text("""
    WITH payers_in_window AS (
        SELECT payer.customer_id
        FROM account recipient
        JOIN pix_transaction pix ON pix.destination_account_id = recipient.id
        JOIN account payer ON payer.id = pix.source_account_id
        WHERE recipient.customer_id = :recipient_customer_id
          AND pix.transaction_date > :payers_since
          AND pix.transaction_date <= :t
        UNION
        SELECT :payer_customer_id
    )
    SELECT count(*) AS prior_attempts,
           count(DISTINCT (pix.transaction_date AT TIME ZONE 'America/Sao_Paulo')::date) AS prior_days,
           count(*) FILTER (WHERE pix.transaction_date > :attempts_since) AS attempts_10m,
           (SELECT count(*) FROM payers_in_window) AS distinct_payers_1h
    FROM account recipient
    JOIN pix_transaction pix ON pix.destination_account_id = recipient.id
    WHERE recipient.customer_id = :recipient_customer_id
      AND pix.transaction_date <= :t
""")

_PAIR_QUERY = text("""
    SELECT count(*) FILTER (WHERE pix.status_id = :approved) AS prior_approved,
           floor(EXTRACT(EPOCH FROM (:t - min(pix.transaction_date) FILTER (WHERE pix.status_id = :approved)))
                 / 86400)::integer AS first_approved_days_ago,
           count(*) FILTER (WHERE pix.status_id IN (:rejected, :two_factor_required)
                              AND pix.transaction_date > :anomaly_since) > 0 AS recent_anomaly
    FROM account payer
    JOIN pix_transaction pix ON pix.source_account_id = payer.id
    JOIN account recipient ON recipient.id = pix.destination_account_id
    WHERE payer.customer_id = :payer_customer_id
      AND recipient.customer_id = :recipient_customer_id
      AND pix.transaction_date <= :t
""")


class PixHistoryRepository:
    """O histórico que o motor lê, contado pelo banco.

    Os quatro métodos são os quatro grupos do Context do motor — pagador,
    aparelho, recebedor e o par — e cada um é UMA ida ao banco. Os campos
    das dataclasses têm os nomes do Context, um a um, na mesma ordem: o
    controller converte com PayerHistory(**asdict(payer_features)).

    As definições valem pros quatro. O Pix atual nunca está aqui: ele só é
    gravado depois da decisão. Anterior é o que já está gravado com
    transaction_date até t, inclusive, sendo t o transaction_date do Pix
    atual. A janela "nos últimos X" é (t − X, t]. Pagador e recebedor são
    o DOCUMENTO, com todas as contas do customer, achadas por
    account_customer_id_idx; o Pix chega pelos índices (eixo,
    transaction_date). Dia é o dia civil de São Paulo, cortado no próprio
    SQL com AT TIME ZONE — o banco roda em UTC, e um Pix às 22h de São
    Paulo já é o dia seguinte lá.

    O repository não decide limiar: as janelas chegam como timedelta, e o
    que fazer com cada contagem é com o motor.

    O SQL está escrito à mão, e não na API de expressões do SQLAlchemy.
    São contagens com FILTER, mediana com WITHIN GROUP e conjuntos com
    UNION e EXCEPT; a mediana com FILTER nem tem como ser escrita pela API
    na versão 2.0.23. Lido como SQL, cada agregado bate com uma linha da
    tabela de features.
    """

    def __init__(self, context: Context) -> None:
        self.session = context.get_or_create_session()
        self.pix_transaction_repository = PixTransactionRepository(context)

    def get_payer_features(
        self,
        *,
        payer_customer_id: int,
        recipient_customer_id: int,
        device_id: str,
        ip: str,
        transaction_date: datetime,
        attempts_window: timedelta,
        amount_window: timedelta,
        new_destinations_window: timedelta,
    ) -> PayerFeatures:
        """O pagador: os Pix anteriores de todas as contas do documento.

        As contagens de rajada e de montante (attempts_5m, amount_1h,
        device_day_amount) pegam qualquer status: a rajada em si é o sinal,
        e contar só approved esconderia o invasor que insiste depois do
        primeiro 2FA. Já o que diz como este cliente é — aparelho e IP
        conhecidos, a mediana, os dias com Pix aprovado — só aprende com
        approved. Um 2FA que ninguém confirmou não pode ensinar ao motor
        que o aparelho é do cliente, senão a segunda tentativa do invasor
        passa limpa.

        new_destinations_10m já inclui o recebedor atual, se ele for
        inédito, e conta uma vez só quem aparece mais de uma vez na janela.
        """
        t = _as_utc(transaction_date)
        row = self.session.execute(_PAYER_QUERY, {
            "payer_customer_id": payer_customer_id,
            "recipient_customer_id": recipient_customer_id,
            "device_id": device_id,
            "ip": ip,
            "t": t,
            "attempts_since": t - attempts_window,
            "amount_since": t - amount_window,
            "new_destinations_since": t - new_destinations_window,
            "approved": self.pix_transaction_repository.get_status_id(PixTransactionStatus.APPROVED),
        }).one()

        # Repare no int() das duas somas. sum de BIGINT volta numeric, que
        # chega como Decimal — e Decimal na evidência de um sinal quebra o
        # JSON da resposta. A conversão fica aqui, e não num CAST no SQL,
        # porque dois Pix no teto do BIGINT somados estourariam o BIGINT.
        return PayerFeatures(
            prior_attempts=row.prior_attempts,
            prior_days=row.prior_days,
            device_known=row.device_known,
            ip_known=row.ip_known,
            attempts_5m=row.attempts_5m,
            amount_1h=int(row.amount_1h),
            approved_count=row.approved_count,
            approved_days=row.approved_days,
            approved_median_amount=row.approved_median_amount,
            new_destinations_10m=row.new_destinations_10m,
            device_known_before_today=row.device_known_before_today,
            device_day_amount=int(row.device_day_amount),
        )

    def get_device_features(
        self,
        *,
        device_id: str,
        payer_customer_id: int,
        transaction_date: datetime,
        payers_window: timedelta,
    ) -> DeviceFeatures:
        """O aparelho, de qualquer pagador.

        distinct_payers_24h já conta o pagador atual — a pergunta do P08 é
        "quantos documentos pagaram por este aparelho, contando este?". O
        UNION com o atual não conta duas vezes quem já está na janela.
        """
        t = _as_utc(transaction_date)
        row = self.session.execute(_DEVICE_QUERY, {
            "device_id": device_id,
            "payer_customer_id": payer_customer_id,
            "t": t,
            "payers_since": t - payers_window,
        }).one()

        return DeviceFeatures(
            prior_attempts=row.prior_attempts,
            distinct_payers_24h=row.distinct_payers_24h,
        )

    def get_recipient_features(
        self,
        *,
        recipient_customer_id: int,
        payer_customer_id: int,
        transaction_date: datetime,
        attempts_window: timedelta,
        payers_window: timedelta,
    ) -> RecipientFeatures:
        """O recebedor, pelo documento: o laranja com três contas em três bancos é um recebedor só.

        distinct_payers_1h já conta o pagador atual, como no aparelho;
        attempts_10m não conta o Pix atual, que o motor soma uma vez.
        """
        t = _as_utc(transaction_date)
        row = self.session.execute(_RECIPIENT_QUERY, {
            "recipient_customer_id": recipient_customer_id,
            "payer_customer_id": payer_customer_id,
            "t": t,
            "attempts_since": t - attempts_window,
            "payers_since": t - payers_window,
        }).one()

        return RecipientFeatures(
            prior_attempts=row.prior_attempts,
            prior_days=row.prior_days,
            attempts_10m=row.attempts_10m,
            distinct_payers_1h=row.distinct_payers_1h,
        )

    def get_pair_features(
        self,
        *,
        payer_customer_id: int,
        recipient_customer_id: int,
        transaction_date: datetime,
        anomaly_window: timedelta,
    ) -> PairFeatures:
        """O par pagador→recebedor, documento a documento.

        first_approved_days_ago conta dias de 24 h, como a idade das
        contas, e não dias de calendário: de segunda às 23h59 até terça às
        0h01 é zero. Sem nenhum approved no par, sai None. recent_anomaly
        olha só rejected e two_factor_required — o próprio motor ter
        desconfiado do par há pouco.
        """
        t = _as_utc(transaction_date)
        status_id = self.pix_transaction_repository.get_status_id
        row = self.session.execute(_PAIR_QUERY, {
            "payer_customer_id": payer_customer_id,
            "recipient_customer_id": recipient_customer_id,
            "t": t,
            "anomaly_since": t - anomaly_window,
            "approved": status_id(PixTransactionStatus.APPROVED),
            "rejected": status_id(PixTransactionStatus.REJECTED),
            "two_factor_required": status_id(PixTransactionStatus.TWO_FACTOR_REQUIRED),
        }).one()

        return PairFeatures(
            prior_approved=row.prior_approved,
            first_approved_days_ago=row.first_approved_days_ago,
            recent_anomaly=row.recent_anomaly,
        )


def _as_utc(transaction_date: datetime) -> datetime:
    # As janelas são subtraídas em UTC. Com um fuso que tem horário de
    # verão (ZoneInfo), t − 1 h no Python é conta de relógio de parede e
    # erra por uma hora na virada; em UTC não existe virada.
    return transaction_date.astimezone(timezone.utc)
