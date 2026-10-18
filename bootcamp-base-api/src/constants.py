import math
import os


SERVICE_ROOT = os.path.abspath(os.path.dirname(__file__))
SCHEMA_PATH = os.path.join(SERVICE_ROOT, "schemas")

APP_ENV = os.environ.get("APP_ENV", "local")
SERVICE_NAME = os.environ.get("SERVICE_NAME", "bootcamp-api")

DATABASE_URL = os.environ.get("DATABASE_URL")
INTERNAL_TOKEN = os.environ.get("INTERNAL_TOKEN")

# O DICT do Banco Central (no compose, o mock dele): de onde vem a estatística
# de segurança do recebedor. Quem usa é o src/connectors/dict_connector.py.
DICT_API_URL = os.environ.get("DICT_API_URL", "http://localhost:1080")
DICT_API_INTERNAL_TOKEN = os.environ.get("DICT_API_INTERNAL_TOKEN", "default_token")


def _read_dict_api_timeout() -> float:
    """O DICT_API_TIMEOUT, conferido na subida.

    Valor que não serve de prazo barra a API de ligar: 0, negativo ou nan
    desligariam a camada 3 em silêncio, e inf quebraria toda consulta.
    """
    raw = os.environ.get("DICT_API_TIMEOUT", "2")
    message = f"DICT_API_TIMEOUT precisa ser um número de segundos maior que zero, e veio {raw!r}."

    try:
        seconds = float(raw)
    except ValueError:
        raise EnvironmentError(message) from None

    if not math.isfinite(seconds) or seconds <= 0:
        raise EnvironmentError(message)

    return seconds


# Quantos segundos esperar pelo DICT. Dois, e não os cinco do connector
# de exemplo do bootcamp: a decisão do Pix tem que sair em poucos
# segundos. O prazo vale por etapa (conectar, e cada leitura), não pela
# chamada inteira, e não cobre o DNS.
DICT_API_TIMEOUT = _read_dict_api_timeout()

# Health endpoints are public; all challenge routes require token.
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
