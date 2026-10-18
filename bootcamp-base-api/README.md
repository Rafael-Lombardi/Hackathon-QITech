# Serviço da API Antifraude Pix

Implementação executável descrita no [README principal](../README.md). Leia a raiz para domínio, camadas, política, banco e cenários; este arquivo cobre apenas operação local.

## Subir a stack

Requisitos: Docker e `docker compose`.

```bash
docker compose up -d --build --wait
until curl -sf http://localhost:1080/security-statistics/00000000000 >/dev/null; do sleep 1; done
```

| Serviço | Porta | Papel |
|---|---:|---|
| `api` | 3000 | API FastAPI |
| `db` | 5432 | PostgreSQL 16 |
| `dict-mock` | 1080 | mock local do DICT |

O banco é criado por [`database/database.sql`](database/database.sql) somente quando o volume nasce. Para recriá-lo:

```bash
docker compose down -v
docker compose up -d --build --wait
```

## Consultar a API

- Swagger: <http://localhost:3000/docs>
- ReDoc: <http://localhost:3000/redoc>
- OpenAPI: <http://localhost:3000/openapi.json>
- Saúde: `GET /health_check` (204)

As rotas Pix exigem `INTERNAL-TOKEN` (padrão local: `default_token`). O contrato de entrada está em [`src/schemas/post_pix_transaction.json`](src/schemas/post_pix_transaction.json).

```bash
curl -s http://localhost:3000/health_check -o /dev/null -w '%{http_code}\n'
curl -s http://localhost:3000/pix/transaction/example-id \
  -H 'INTERNAL-TOKEN: default_token'
```

## Testes

Os cenários completos ficam em [`tests/integration/pix/`](tests/integration/pix/) e são explicados no README principal.

```bash
cp .env.example .env
uv venv --python 3.11 .venv
uv pip install -r requirements-dev.txt
source .venv/bin/activate
APP_ENV=test docker compose up -d --build --force-recreate --wait
pytest
```

O `.env.example` já traz `DATABASE_URL`, `INTERNAL_TOKEN`, `DICT_API_URL` e
`SERVER_LOCALHOST`. O `.env` é local e ignorado pelo Git. Para não depender de
venv ou export manual, use `just test-suite`.

Atalho equivalente, incluindo a recriação do banco local:

```bash
just test-suite
```

Para selecionar um cenário:

```bash
pytest --collect-only -q tests/integration/pix
pytest -vv tests/integration/pix/test_decisoes.py
```

## Estrutura

- `src/`: aplicação, controller, motor, modelos e persistência;
- `database/`: schema inicial do PostgreSQL;
- `mockserver/`: respostas do DICT mockado;
- `tests/integration/pix/`: cenários executáveis;
- [`../docs/README-interno.md`](../docs/README-interno.md): manutenção detalhada.
