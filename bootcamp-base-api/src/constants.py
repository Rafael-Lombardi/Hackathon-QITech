import os


SERVICE_ROOT = os.path.abspath(os.path.dirname(__file__))
SCHEMA_PATH = os.path.join(SERVICE_ROOT, "schemas")

APP_ENV = os.environ.get("APP_ENV", "local")
SERVICE_NAME = os.environ.get("SERVICE_NAME", "bootcamp-api")

DATABASE_URL = os.environ.get("DATABASE_URL")
INTERNAL_TOKEN = os.environ.get("INTERNAL_TOKEN")

# O DICT do Banco Central — no nosso caso, o mock dele, o serviço
# dict-mock do docker-compose.yml. É dele o trust factor do recebedor (a
# camada 3), e quem fala com ele é o src/connectors/dict_connector.py. O
# endereço vem do ambiente, como tudo aqui: no compose é
# http://dict-mock:1080; o padrão abaixo serve a quem roda a API direto
# na máquina, com o mock de pé no Docker.
DICT_API_URL = os.environ.get("DICT_API_URL", "http://localhost:1080")
DICT_API_INTERNAL_TOKEN = os.environ.get("DICT_API_INTERNAL_TOKEN", "default_token")

# Quantos segundos esperar pelo DICT antes de desistir. Dois, e não os
# cinco do connector de exemplo do bootcamp: a decisão do Pix tem que
# sair em poucos segundos, e o DICT é só uma das etapas dela. Estourou,
# a camada 3 fica "unavailable" e o Pix segue sem ela.
DICT_API_TIMEOUT = float(os.environ.get("DICT_API_TIMEOUT", "2"))

# Health endpoints are public; all challenge routes require token.
BYPASS_ENDPOINTS = [
    "/",
    "/health_check",
]

# Repare que as três DICT_* ficam FORA desta lista, de propósito. Sem o
# DICT a API continua decidindo: a camada 3 sai "unavailable" e o Pix é
# decidido com as outras duas. Exigir o endereço aqui faria de uma peça
# que o motor sabe dispensar o motivo pra API inteira não ligar. E o
# defeito não passa calado: toda resposta traz o DICT01 "unavailable", e
# toda consulta perdida deixa uma linha DICT UNAVAILABLE no log.
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
