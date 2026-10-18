"""O histórico que o motor antifraude lê, contado pelo banco.

São cinco métodos, um por grupo do Context do motor — pagador, aparelho,
recebedor, o par e a localização —, cada um numa ida ao banco, e cada um
devolve direto o tipo de antifraud/contract.py. O porquê de cada campo
mora lá: por que conhecido é só com approved, por que os distintos já
contam o atual, onde cabe teto. Aqui fica o como contar.

As definições valem pros cinco. O relógio é transaction_date, o tempo do
negócio: o payload vem do sistema do banco, e não de um cliente qualquer,
e é essa data que diz quando o Pix aconteceu. Contar pelo relógio do
servidor (received_at) faria dez Pix de dez dias, mandados hoje, virarem
uma rajada de dez em cinco minutos — e ninguém reproduziria um cenário
sem mexer no relógio da máquina. Com o tempo do negócio, o histórico é
reproduzível por quem manda as datas no payload, e um Pix que chegar
atrasado cai no lugar certo.

Anterior é o que já está gravado com transaction_date até t, inclusive,
sendo t o transaction_date do Pix atual — ele mesmo só é gravado depois
da decisão, e por isso nunca se conta. Inclusive, e não antes: dois Pix
do mesmo pagador com o mesmo instante, em corrida, passam um de cada vez
pela trava, e o segundo precisa enxergar o primeiro. "Nos últimos X" é a
janela (t - X, t]. Dia é o dia civil de São Paulo, cortado no SQL com AT
TIME ZONE e o LOCAL_TIMEZONE do motor: o banco roda em UTC, e um Pix às
22h de São Paulo já é o dia seguinte lá. Pagador e recebedor são o
DOCUMENTO, e o documento é a lista de contas dele: o pagador e o par
recebem a lista pronta do controller (AccountRepository.ids_of), o
recebedor resolve as suas no SQL, e os Pix de cada conta saem dos
índices de conta de origem, de conta de destino e do par.

count(*) FILTER (WHERE ...) conta só as linhas que passam no WHERE, e
várias contagens assim saem da mesma leitura. percentile_cont(0.5) é a
mediana — com quantidade par, a média dos dois do meio.

O SQL está escrito à mão, e não na API de expressões do SQLAlchemy: a
mediana com FILTER nem tem como ser escrita por ela na versão 2.0.23, e
lido como SQL cada agregado bate com um campo do contrato.
"""

from datetime import datetime, timedelta
from math import asin, cos, radians, sin, sqrt

from sqlalchemy import text

from antifraud import policy
from antifraud.contract import LOCAL_TIMEZONE, DeviceHistory, PairHistory, PayerHistory, RecipientHistory
from database import Context
from models import PixTransactionStatus
from repositories.pix_transaction_repository import PixTransactionRepository

# payer_pix lê o histórico inteiro do pagador uma vez, e cada campo é um
# agregado sobre ela. As contas do pagador chegam como lista
# (:payer_accounts), e não por JOIN com account: com os ids à vista o
# planejador estima o tamanho certo do histórico (pelo JOIN ele chutava
# a média, 26 Pix, pra um pagador de 5 mil), e o índice cobrindo da conta
# de origem responde sem ir ao heap. A linha leva a conta de destino, e
# não o documento do recebedor: ir de conta a documento custa uma busca
# por linha, e só new_destinations precisa disso. Ali o caminho é o
# contrário: primeiro as contas dos candidatos, que são poucas, e depois
# os approved anteriores à janela pra alguma delas. Repare no
# MATERIALIZED: sem ele o Postgres embute candidate_account na consulta,
# reordena o JOIN e volta a buscar a conta de cada approved — medido num
# pagador com 3 mil Pix, 7.916 páginas em vez de 215.
_PAYER_QUERY = text("""
    WITH payer_pix AS (
        SELECT pix.transaction_date,
               (pix.transaction_date AT TIME ZONE :local_timezone)::date AS local_day,
               pix.amount,
               pix.status_id,
               pix.device_id,
               pix.ip,
               pix.destination_account_id
        FROM pix_transaction pix
        WHERE pix.source_account_id = ANY(:payer_accounts)
          AND pix.transaction_date <= :t
    ),
    candidates AS (
        -- os recebedores da janela, mais o atual
        SELECT recipient.customer_id
        FROM payer_pix
        JOIN account recipient ON recipient.id = payer_pix.destination_account_id
        WHERE payer_pix.transaction_date > :new_destinations_since
        UNION
        SELECT :recipient_customer_id
    ),
    candidate_account AS MATERIALIZED (
        SELECT candidates.customer_id, recipient.id
        FROM candidates
        JOIN account recipient ON recipient.customer_id = candidates.customer_id
    ),
    new_destinations AS (
        -- os candidatos, menos os que já tinham approved deste pagador antes da janela
        SELECT customer_id FROM candidates
        EXCEPT
        SELECT candidate_account.customer_id
        FROM candidate_account
        JOIN payer_pix ON payer_pix.destination_account_id = candidate_account.id
        WHERE payer_pix.status_id = :approved
          AND payer_pix.transaction_date <= :new_destinations_since
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
                              AND local_day < (:t AT TIME ZONE :local_timezone)::date) > 0
               AS device_known_before_today,
           coalesce(sum(amount) FILTER (WHERE device_id = :device_id
                                          AND local_day = (:t AT TIME ZONE :local_timezone)::date), 0)
               AS device_day_amount
    FROM payer_pix
""")

# prior_attempts sai de um EXISTS, 0 ou 1: um aparelho com milhares de
# Pix custa uma descida no índice. O JOIN com account, pra chegar ao
# documento de cada pagador, fica restrito à janela.
_DEVICE_QUERY = text("""
    WITH payers_in_window AS (
        -- os pagadores da janela, mais o atual
        SELECT payer.customer_id
        FROM pix_transaction pix
        JOIN account payer ON payer.id = pix.source_account_id
        WHERE pix.device_id = :device_id
          AND pix.transaction_date > :payers_since
          AND pix.transaction_date <= :t
        UNION
        SELECT :payer_customer_id
    )
    SELECT EXISTS (
               SELECT 1
               FROM pix_transaction
               WHERE device_id = :device_id
                 AND transaction_date <= :t
           )::integer AS prior_attempts,
           (SELECT count(*) FROM payers_in_window) AS distinct_payers_24h
""")

# prior_attempts e prior_days param no teto, e um recebedor com 30 mil
# Pix custa o mesmo que um com 30. prior_attempts é o count de um LIMIT.
# prior_days é a parte difícil: contar dias distintos sem ler os Pix um a
# um. A CTE recursiva desce o histórico de dia em dia. A primeira volta
# pega o último Pix até t e guarda a meia-noite, em São Paulo, do dia
# dele. Cada volta seguinte pega o último Pix antes dessa meia-noite, que
# cai no dia com Pix logo anterior — os dias vazios ficam pulados. Cada
# busca é um ORDER BY ... DESC LIMIT 1 por conta, uma descida no índice
# de conta de destino; uma volta é um dia, e o laço para em days_cap.
_RECIPIENT_QUERY = text("""
    WITH RECURSIVE recipient_account AS (
        SELECT id FROM account WHERE customer_id = :recipient_customer_id
    ),
    active_day(day_start, n) AS (
        -- o dia do último Pix até t
        SELECT date_trunc('day', max(last_pix.transaction_date), :local_timezone), 1
        FROM recipient_account
        CROSS JOIN LATERAL (
            SELECT pix.transaction_date
            FROM pix_transaction pix
            WHERE pix.destination_account_id = recipient_account.id
              AND pix.transaction_date <= :t
            ORDER BY pix.transaction_date DESC
            LIMIT 1
        ) AS last_pix
        HAVING max(last_pix.transaction_date) IS NOT NULL
        UNION ALL
        -- o dia do último Pix antes da meia-noite do dia achado na volta anterior
        SELECT date_trunc('day', previous.transaction_date, :local_timezone), active_day.n + 1
        FROM active_day
        CROSS JOIN LATERAL (
            SELECT max(last_pix.transaction_date) AS transaction_date
            FROM recipient_account
            CROSS JOIN LATERAL (
                SELECT pix.transaction_date
                FROM pix_transaction pix
                WHERE pix.destination_account_id = recipient_account.id
                  AND pix.transaction_date < active_day.day_start
                ORDER BY pix.transaction_date DESC
                LIMIT 1
            ) AS last_pix
        ) AS previous
        WHERE active_day.n < :days_cap
          AND previous.transaction_date IS NOT NULL
    ),
    payers_in_window AS (
        -- os pagadores da janela, mais o atual
        SELECT payer.customer_id
        FROM recipient_account
        JOIN pix_transaction pix ON pix.destination_account_id = recipient_account.id
        JOIN account payer ON payer.id = pix.source_account_id
        WHERE pix.transaction_date > :payers_since
          AND pix.transaction_date <= :t
        UNION
        SELECT :payer_customer_id
    )
    SELECT (SELECT count(*)
            FROM (SELECT 1
                  FROM recipient_account
                  JOIN pix_transaction pix ON pix.destination_account_id = recipient_account.id
                  WHERE pix.transaction_date <= :t
                  LIMIT :attempts_cap) AS capped) AS prior_attempts,
           (SELECT count(*) FROM active_day) AS prior_days,
           (SELECT count(*)
            FROM recipient_account
            JOIN pix_transaction pix ON pix.destination_account_id = recipient_account.id
            WHERE pix.transaction_date > :attempts_since
              AND pix.transaction_date <= :t) AS attempts_10m,
           (SELECT count(*) FROM payers_in_window) AS distinct_payers_1h
""")

# As contas dos dois documentos vêm como lista: é o que deixa o par sair
# do índice do par numa descida só. Pelo JOIN com account, o Postgres lia
# o histórico inteiro do pagador e buscava a conta de cada Pix pra saber
# o documento do destino — 20 mil páginas num pagador de 5 mil Pix; assim
# são 19.
_PAIR_QUERY = text("""
    SELECT count(*) FILTER (WHERE pix.status_id = :approved) AS prior_approved,
           min(pix.transaction_date) FILTER (WHERE pix.status_id = :approved) AS first_approved_at,
           count(*) FILTER (WHERE pix.status_id IN (:rejected, :two_factor_required)
                              AND pix.transaction_date > :anomaly_since) > 0 AS recent_anomaly
    FROM pix_transaction pix
    WHERE pix.source_account_id = ANY(:payer_accounts)
      AND pix.destination_account_id = ANY(:recipient_accounts)
      AND pix.transaction_date <= :t
""")

# Os Pix aprovados com localização dos últimos 90 dias, fora das últimas
# 24 h: o dia civil vai junto porque a cobertura do G01 se mede em dias,
# e o corte de 24 h impede que a rajada de agora ensine o lugar de agora.
# O LIMIT segura o pior caso; mil observações bastam pra formar padrão.
_LOCATION_QUERY = text("""
    SELECT pix.latitude, pix.longitude,
           (pix.transaction_date AT TIME ZONE :local_timezone)::date AS local_day
    FROM account payer
    JOIN pix_transaction pix ON pix.source_account_id = payer.id
    WHERE payer.customer_id = :customer_id
      AND pix.status_id = :approved
      AND pix.transaction_date > :since
      AND pix.transaction_date <= :cutoff
      AND pix.latitude IS NOT NULL AND pix.longitude IS NOT NULL
    ORDER BY pix.transaction_date DESC
    LIMIT 1000
""")


def _distance_km(latitude_a: float, longitude_a: float, latitude_b: float, longitude_b: float) -> float:
    lat1, lat2 = radians(latitude_a), radians(latitude_b)
    dlat = lat2 - lat1
    dlon = radians(longitude_b) - radians(longitude_a)
    a = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
    return 6371.0 * 2 * asin(sqrt(a))


class PixHistoryRepository:
    """As consultas de histórico, uma por grupo do Context do motor."""

    def __init__(self, context: Context) -> None:
        self.session = context.db_session
        self.pix_transaction_repository = PixTransactionRepository(context)

    def get_location_distance_km(
        self, *, payer_customer_id: int, at: datetime,
        latitude: float | None, longitude: float | None,
    ) -> float | None:
        """Quilômetros até o lugar de sempre mais perto, ou None sem padrão formado.

        Os Pix localizados são agrupados em centros de LOCATION_CLUSTER_RADIUS_KM,
        e só centro visto em LOCATION_MIN_DAYS_PER_CENTER dias ou mais
        conta como lugar de sempre. Sem LOCATION_MIN_OBSERVATIONS Pix em
        LOCATION_MIN_DAYS dias, ou sem localização no Pix atual, não há
        com o que comparar: None, e o G01 sai unavailable.
        """
        if latitude is None or longitude is None:
            return None
        rows = self.session.execute(_LOCATION_QUERY, {
            "customer_id": payer_customer_id,
            "approved": self.pix_transaction_repository.get_status_id(PixTransactionStatus.APPROVED),
            "since": at - policy.LOCATION_HISTORY_WINDOW,
            "cutoff": at - policy.LOCATION_RECENCY_GAP,
            "local_timezone": LOCAL_TIMEZONE.key,
        }).all()
        if len(rows) < policy.LOCATION_MIN_OBSERVATIONS or len({row[2] for row in rows}) < policy.LOCATION_MIN_DAYS:
            return None

        centers: list[dict] = []
        for point_lat, point_lon, local_day in rows:
            point = (float(point_lat), float(point_lon))
            assigned = None
            for center in centers:
                if _distance_km(point[0], point[1], center["lat"], center["lon"]) <= policy.LOCATION_CLUSTER_RADIUS_KM:
                    assigned = center
                    break
            if assigned is None:
                centers.append({"lat": point[0], "lon": point[1], "days": {local_day}, "count": 1})
            else:
                count = assigned["count"]
                assigned["lat"] = (assigned["lat"] * count + point[0]) / (count + 1)
                assigned["lon"] = (assigned["lon"] * count + point[1]) / (count + 1)
                assigned["days"].add(local_day)
                assigned["count"] = count + 1
        qualified = [center for center in centers if len(center["days"]) >= policy.LOCATION_MIN_DAYS_PER_CENTER]
        if not qualified:
            return None
        return min(_distance_km(latitude, longitude, center["lat"], center["lon"]) for center in qualified)

    def get_payer_features(
        self,
        *,
        payer_accounts: list[int],
        recipient_customer_id: int,
        device_id: str,
        ip: str,
        at: datetime,
        attempts_window: timedelta,
        amount_window: timedelta,
        new_destinations_window: timedelta,
    ) -> PayerHistory:
        """O pagador: os Pix anteriores de todas as contas do documento.

        payer_accounts são os ids dessas contas, prontos. O porquê de cada
        campo está no PayerHistory, no contrato do motor.
        """
        row = self.session.execute(_PAYER_QUERY, {
            "payer_accounts": payer_accounts,
            "recipient_customer_id": recipient_customer_id,
            "device_id": device_id,
            "ip": ip,
            "t": at,
            "attempts_since": at - attempts_window,
            "amount_since": at - amount_window,
            "new_destinations_since": at - new_destinations_window,
            "approved": self.pix_transaction_repository.get_status_id(PixTransactionStatus.APPROVED),
            "local_timezone": LOCAL_TIMEZONE.key,
        }).one()

        # Repare no int() das duas somas: sum de BIGINT volta numeric, que
        # chega como Decimal, e o contrato pede int. A conversão fica aqui,
        # e não num CAST no SQL, porque dois Pix no teto do BIGINT somados
        # estourariam o BIGINT.
        return PayerHistory(
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
        at: datetime,
        payers_window: timedelta,
    ) -> DeviceHistory:
        """O aparelho, de qualquer pagador. Os porquês estão no DeviceHistory, no contrato do motor."""
        row = self.session.execute(_DEVICE_QUERY, {
            "device_id": device_id,
            "payer_customer_id": payer_customer_id,
            "t": at,
            "payers_since": at - payers_window,
        }).one()

        return DeviceHistory(
            prior_attempts=row.prior_attempts,
            distinct_payers_24h=row.distinct_payers_24h,
        )

    def get_recipient_features(
        self,
        *,
        recipient_customer_id: int,
        payer_customer_id: int,
        at: datetime,
        attempts_window: timedelta,
        payers_window: timedelta,
        attempts_cap: int,
        days_cap: int,
    ) -> RecipientHistory:
        """O recebedor, pelo documento, com teto em prior_attempts e prior_days.

        Os tetos chegam como as janelas, por parâmetro. Onde cabe teto, e
        por quê, o RecipientHistory diz, no contrato do motor.
        """
        row = self.session.execute(_RECIPIENT_QUERY, {
            "recipient_customer_id": recipient_customer_id,
            "payer_customer_id": payer_customer_id,
            "t": at,
            "attempts_since": at - attempts_window,
            "payers_since": at - payers_window,
            "attempts_cap": attempts_cap,
            "days_cap": days_cap,
            "local_timezone": LOCAL_TIMEZONE.key,
        }).one()

        return RecipientHistory(
            prior_attempts=row.prior_attempts,
            prior_days=row.prior_days,
            attempts_10m=row.attempts_10m,
            distinct_payers_1h=row.distinct_payers_1h,
        )

    def get_pair_features(
        self,
        *,
        payer_accounts: list[int],
        recipient_accounts: list[int],
        at: datetime,
        anomaly_window: timedelta,
    ) -> PairHistory:
        """O par pagador→recebedor, documento a documento: todas as contas de um pra todas as do outro.

        O SQL devolve o primeiro approved do par, e os dias saem aqui, com
        a conta da idade das contas: (transaction_date - primeiro).days.
        Sem nenhum approved no par, None.
        """
        status_id = self.pix_transaction_repository.get_status_id
        row = self.session.execute(_PAIR_QUERY, {
            "payer_accounts": payer_accounts,
            "recipient_accounts": recipient_accounts,
            "t": at,
            "anomaly_since": at - anomaly_window,
            "approved": status_id(PixTransactionStatus.APPROVED),
            "rejected": status_id(PixTransactionStatus.REJECTED),
            "two_factor_required": status_id(PixTransactionStatus.TWO_FACTOR_REQUIRED),
        }).one()

        first_approved_at = row.first_approved_at
        return PairHistory(
            prior_approved=row.prior_approved,
            first_approved_days_ago=None if first_approved_at is None else (at - first_approved_at).days,
            recent_anomaly=row.recent_anomaly,
        )
