import math
import os


SERVICE_ROOT = os.path.abspath(os.path.dirname(__file__))
SCHEMA_PATH = os.path.join(SERVICE_ROOT, "schemas")

APP_ENV = os.environ.get("APP_ENV", "local")
SERVICE_NAME = os.environ.get("SERVICE_NAME", "bootcamp-api")

DATABASE_URL = os.environ.get("DATABASE_URL")
INTERNAL_TOKEN = os.environ.get("INTERNAL_TOKEN")

# As três fontes que o controller consulta antes de decidir, cada uma
# atrás do próprio connector em src/connectors/: o DICT do Banco Central
# (estatística e ocorrências de segurança de um documento), o cadastro de
# aparelhos do banco (o par pagador–aparelho está cadastrado?) e as
# restrições internas (alguém desta transação está bloqueado?). No compose as três são o mesmo MockServer, o serviço
# dict-mock; trocar o mock pela fonte de verdade é mudar a URL dela, e
# só. Prazo e token são um só pras três: são o mesmo tipo de conversa,
# entre serviços internos, e ninguém quer três prazos pra afinar.
DICT_API_URL = os.environ.get("DICT_API_URL", "http://localhost:1080")
DEVICE_REGISTRY_URL = os.environ.get("DEVICE_REGISTRY_URL", "http://localhost:1080")
RESTRICTIONS_URL = os.environ.get("RESTRICTIONS_URL", "http://localhost:1080")
SOURCES_INTERNAL_TOKEN = os.environ.get("SOURCES_INTERNAL_TOKEN", "default_token")


def _read_sources_timeout() -> float:
    """O SOURCES_TIMEOUT, conferido na subida.

    Valor que não serve de prazo barra a API de ligar: 0, negativo ou nan
    desligariam as fontes em silêncio, e inf quebraria toda consulta.
    """
    raw = os.environ.get("SOURCES_TIMEOUT", "2")
    message = f"SOURCES_TIMEOUT precisa ser um número de segundos maior que zero, e veio {raw!r}."

    try:
        seconds = float(raw)
    except ValueError:
        raise EnvironmentError(message) from None

    if not math.isfinite(seconds) or seconds <= 0:
        raise EnvironmentError(message)

    return seconds


# Quantos segundos esperar por cada fonte. Dois, e não os cinco do
# connector de exemplo do bootcamp: a decisão do Pix tem que sair em
# poucos segundos, e são três consultas por Pix. O prazo vale por etapa
# (conectar, e cada leitura), não pela chamada inteira, e não cobre o
# DNS.
SOURCES_TIMEOUT = _read_sources_timeout()

# A raiz e o health check são públicos; o resto pede o token.
BYPASS_ENDPOINTS = [
    "/",
    "/health_check",
]

REQUIRED_VARIABLES = ["DATABASE_URL", "INTERNAL_TOKEN"]


def check_variables():
    missing = []
    for name in REQUIRED_VARIABLES:
        if not globals().get(name):
            missing.append(name)

    if missing:
        raise EnvironmentError(
            f"Faltam variáveis de ambiente: {', '.join(missing)}. "
            "Rodando com 'docker compose up' elas já vêm preenchidas. "
            "Fora do Docker, copie o .env.example para .env."
        )
