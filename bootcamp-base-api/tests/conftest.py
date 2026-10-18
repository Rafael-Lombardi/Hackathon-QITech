from pathlib import Path
from os import path, environ

root = Path(__file__).resolve().parents[1]

# Os testes de integração também exercitam controller/repository diretamente
# quando precisam simular falha de infraestrutura ou concorrência de sessão.
# Mantém o layout src executável sem exigir PYTHONPATH manual.
import sys

sys.path.insert(0, str(root / "src"))

if not environ.get("APP_ENV") or environ.get("APP_ENV") == "local":
    from dotenv import load_dotenv

    load_dotenv(path.join(str(root), ".env"))

    if environ.get("SERVER_LOCALHOST") is None:
        environ["SERVER_LOCALHOST"] = "0.0.0.0"

# Os imports abaixo vêm depois do .env de propósito: os helpers leem porta
# e token do ambiente, e precisam ler o que o .env acabou de preencher.
import time

import pytest
import requests

from tests.utils.dict_mock import DictMock, mock_base_url, security_statistics_path
from tests.utils.requisition import api_base_url

# Quanto esperar a stack antes de desistir. O docker compose up --wait já
# espera a API ficar saudável; o mock do DICT não tem healthcheck (a
# imagem não tem shell) e leva de 2 a 18 s pra atender depois de subir
# (mockserver/README.md). Por isso a fixture pergunta de meio em meio
# segundo, em vez de um sleep fixo: com a stack já de pé, o primeiro
# teste nem espera.
STACK_TIMEOUT = 60

# Documento que nenhum teste usa (os aleatórios nunca repetem um dígito
# só): serve pra perguntar ao mock se a expectativa padrão foi carregada.
PROBE_DOCUMENT = "00000000000"

STACK_DOWN = (
    "A stack não ficou de pé em {timeout} s:\n{pending}\n"
    "Suba tudo antes de rodar a suíte:  docker compose up -d --build --wait"
)


@pytest.fixture(scope="session", autouse=True)
def stack_ready():
    """Espera a API e o mock do DICT antes do primeiro teste.

    Todo teste desta suíte faz HTTP contra a stack de pé. Sem ela, cada um
    falharia sozinho com um erro de conexão — cem vezes a mesma notícia.
    Aqui a notícia sai uma vez, com o comando que resolve, e a suíte para.
    """
    deadline = time.monotonic() + STACK_TIMEOUT
    pending = _pending()
    while pending and time.monotonic() < deadline:
        time.sleep(0.5)
        pending = _pending()

    if pending:
        pytest.exit(STACK_DOWN.format(timeout=STACK_TIMEOUT, pending="\n".join(pending)))


@pytest.fixture
def dict_mock():
    """O roteiro do mock do DICT para um teste, apagado quando o teste acaba.

    Apaga pelo id, passe o teste ou falhe. O porquê — o limite de
    expectativas do mock — está em mockserver/README.md.
    """
    mock = DictMock()
    yield mock
    mock.clear()


def _pending() -> list[str]:
    """O que ainda não respondeu como deveria, uma linha por peça."""
    checks = [
        ("API", "GET", f"{api_base_url()}/health_check", _no_content),
        ("mock do DICT", "PUT", f"{mock_base_url()}/mockserver/status", _ok),
        ("expectativa padrão do mock", "GET", f"{mock_base_url()}{security_statistics_path(PROBE_DOCUMENT)}", _high_statistics),
    ]
    pending = []
    for piece, method, url, accept in checks:
        try:
            response = requests.request(method, url, timeout=2)
        except requests.RequestException as error:
            pending.append(f"  • {piece} ({method} {url}): {type(error).__name__}")
            continue
        problem = accept(response)
        if problem:
            pending.append(f"  • {piece} ({method} {url}): {problem}")
    return pending


def _no_content(response) -> str | None:
    return None if response.status_code == 204 else f"HTTP {response.status_code}, esperava 204"


def _ok(response) -> str | None:
    return None if response.status_code == 200 else f"HTTP {response.status_code}, esperava 200"


def _high_statistics(response) -> str | None:
    # Os cenários que não tratam da camada 3 contam com o DICT01 calado, e
    # isso pede a expectativa padrão de mockserver/dict-expectations.json
    # respondendo confiança de 30 pra cima (a padrão é 70) pra qualquer
    # documento.
    try:
        body = response.json()
    except ValueError:
        body = None
    statistics = body.get("security_statistics") if isinstance(body, dict) else None
    if response.status_code == 200 and isinstance(statistics, int) and statistics >= 30:
        return None
    return f"HTTP {response.status_code} {response.text[:80]!r}, esperava 200 com security_statistics >= 30"
