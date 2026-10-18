"""O relógio dos cenários: o tempo escolhido pelo teste.

A API conta as janelas de histórico pelo relógio do servidor (RFC §2), e
a stack de teste deixa cada requisição dizer que horas são, pelo header
X-Test-Received-At — o post_pix de tests/utils/pix.py manda, por padrão,
a própria transaction_date do payload. É isso que deixa um teste montar
"4 Pix nos últimos 5 minutos" ou "10 Pix em 7 dias" sem esperar nada: ele
escolhe as datas e manda. Nenhum teste da suíte depende de que horas são
agora.
"""

from datetime import datetime, timedelta, timezone

# São Paulo não tem horário de verão desde 2019: é -03:00 o ano inteiro.
# Um fuso fixo basta, e não pede a base de fusos (tzdata) na máquina de
# quem roda o teste.
SAO_PAULO = timezone(timedelta(hours=-3), "America/Sao_Paulo")

# O instante zero dos cenários: quarta-feira, 14/10/2026, 14h em São
# Paulo. Dia útil, horário comercial, longe da janela noturna do L01 (20h
# às 6h). O histórico de cada cenário é montado para trás a partir daqui.
T0 = datetime(2026, 10, 14, 14, 0, tzinfo=SAO_PAULO)

# Conta aberta há mais de seis anos: não chega perto do P01 nem do R01,
# que olham contas com menos de 7 dias.
MATURE_OPENING_DATE = datetime(2020, 1, 15, 10, 0, tzinfo=SAO_PAULO)

# O lastro que o P06 pede antes de comparar com a mediana: 10 Pix
# aprovados em 7 dias civis. Os números são quantos Pix por dia, do dia
# mais antigo pra véspera, no formato do spread_over_days. Os dois de
# baixo são o lado de fora, um por pré-condição.
TEN_OVER_SEVEN_DAYS = [2, 2, 2, 1, 1, 1, 1]
NINE_OVER_SEVEN_DAYS = [2, 2, 1, 1, 1, 1, 1]
TEN_OVER_SIX_DAYS = [2, 2, 2, 2, 1, 1]


def iso(moment: datetime) -> str:
    """O formato dos cenários: até os segundos, com o fuso escrito."""
    return moment.isoformat(timespec="seconds")


def at(moment: datetime, hour: int, minute: int = 0) -> datetime:
    """O mesmo dia civil de `moment`, em outra hora de São Paulo."""
    local = moment.astimezone(SAO_PAULO)
    return local.replace(hour=hour, minute=minute, second=0, microsecond=0)


def days_before(moment: datetime, count: int) -> list[datetime]:
    """Um instante por dia, à mesma hora, nos `count` dias antes de `moment`.

    Do mais antigo ao mais novo, que é a ordem em que se manda.
    """
    return [moment - timedelta(days=days) for days in range(count, 0, -1)]


def days_ago(moment: datetime, *days: int) -> list[datetime]:
    """Os instantes de tantos dias antes de `moment`: days_ago(T0, 10, 5, 1)."""
    return [moment - timedelta(days=count) for count in days]


def minutes_before(moment: datetime, count: int) -> list[datetime]:
    """Um instante por minuto, nos `count` minutos antes de `moment`."""
    return [moment - timedelta(minutes=minutes) for minutes in range(count, 0, -1)]


def minutes_ago(moment: datetime, *minutes: int) -> list[datetime]:
    """Os instantes de tantos minutos antes de `moment`: minutes_ago(T0, 50, 40, 30)."""
    return [moment - timedelta(minutes=count) for count in minutes]


def spread_over_days(
    per_day: list[int],
    end: datetime = T0,
    first_hour: int = 9,
    every: timedelta = timedelta(hours=1),
) -> list[datetime]:
    """Pix espalhados por dias civis seguidos, terminando na véspera de `end`.

    per_day=[2, 1, 1] dá dois Pix três dias antes de `end` (9h e 10h), um
    dois dias antes e um na véspera. Uma hora entre os Pix do mesmo dia
    deixa a rajada (P04, 5 min) de fora; a janela de 1 h do P05 é fechada
    no começo, e o Pix de exatamente uma hora antes conta — com os
    valores pequenos das sementes, longe dos R$ 8.000.
    """
    first_day = at(end, first_hour) - timedelta(days=len(per_day))
    moments = []
    for day, count in enumerate(per_day):
        start = first_day + timedelta(days=day)
        moments.extend(start + every * position for position in range(count))
    return moments
