# Antifraude Pix — a API

A API do motor antifraude Pix: recebe um Pix em `POST /pix/transaction`, decide entre `approved`, `two_factor_required` e `rejected`, grava o Pix com a decisão explicada e devolve a análise. `GET /pix/transaction/{id}` devolve o que foi gravado. É **Python 3.11 + FastAPI**, com **PostgreSQL 16**, rodando em **Docker**, e nasceu do Base Service do bootcamp. Para decidir, ela consulta o DICT, o diretório do Banco Central que liga chave Pix a conta; aqui quem faz o papel dele é um mock, um servidor de mentira que devolve as respostas combinadas com ele (seção 4).

As decisões deste serviço seguem a política `v0.5.0`, descrita na [RFC v0.5 do Notion](https://app.notion.com/p/RFC-v0-5-Modelagem-de-Risco-Antifraude-Pix-1-3fc6b8cbe2cc8022aeeaf6e39530a099). Evidência antifraude do DICT é fonte separada de estatísticas (`DICT01`) e marcações (`DICT02`/`DICT03`); `clear` ou indisponibilidade não significam segurança.

As regras, o porquê de cada parâmetro e a tabela de decisão estão no [README da raiz](../README.md). Este é o do serviço: como subir, o que cada rota responde, a configuração, o mock, os testes e o caminho de uma requisição.

---

## 0. O que você precisa ter instalado

| O quê | Para quê | Como conferir |
|---|---|---|
| **Docker**, com o `docker compose` | roda a API, o banco e o mock | `docker compose version` |
| **Git** | trazer o projeto | `git --version` |
| **uv**, que instala o Python 3.11 dos testes | rodar os testes (seção 5) | `uv --version` |

O uv instala com `curl -LsSf https://astral.sh/uv/install.sh | sh`.

**Por que o 3.11, se tudo roda no Docker?** Porque os testes rodam fora dele, na sua máquina, contra a API de pé. É a versão da imagem, e uma versão nova demais quebra a instalação: o `psycopg2-binary` 2.9.10 não tem pacote pronto para o Python 3.14, que já é o padrão de distribuições novas. O uv baixa o 3.11 sem mexer no Python da máquina.

---

## 1. Subindo

```bash
docker compose up -d --build --wait
until curl -sf http://localhost:1080/security-statistics/00000000000 > /dev/null; do sleep 1; done
```

Não precisa criar nem copiar arquivo nenhum antes: toda configuração tem valor padrão no `docker-compose.yml`. Sobem três serviços:

| Serviço | O que é | Porta |
|---|---|---|
| `api` | a API. Roda com `--reload`: mudou um arquivo de `src/`, ela recarrega sozinha | 3000 |
| `db` | o PostgreSQL, com as tabelas do [`database/database.sql`](database/database.sql) | 5432 |
| `dict-mock` | o DICT de mentira, um MockServer (seção 4) | 1080 |

O `--wait` devolve o terminal quando o `/health_check` da API responde e o banco aceita conexão. O mock conta como pronto assim que o container roda: a imagem dele não tem shell para um healthcheck, e ele ainda leva de 2 a 18 s para atender (medido). É isso que a segunda linha espera; até lá, o DICT01 sai `unavailable` e o Pix é decidido sem a camada 3.

```bash
curl -i http://localhost:3000/health_check    # 204, sem corpo
docker compose logs -f api                     # o log da API
docker compose down                            # desliga
docker compose down -v                         # desliga e apaga o banco
```

Repare no `-v` do último: ele apaga o volume, o disco do banco, e é o único jeito de o `database.sql` rodar de novo. O arquivo roda uma vez só, quando o banco nasce.

---

## 2. As rotas

| Método e rota | O que faz | Responde |
|---|---|---|
| `GET /` | diz qual serviço é este | `200` com o nome e o id do processo |
| `GET /health_check` | diz se a API está de pé | `204`, sem corpo |
| `POST /pix/transaction` | analisa um Pix | `201` com a análise nova; `200` com a análise gravada, num reenvio idêntico |
| `GET /pix/transaction/{id}` | devolve a análise gravada; `{id}` é o `id` do payload | `200` |

As duas de cima são abertas — o `/health_check` porque quem o consulta é o próprio Docker, que não tem como mandar cabeçalho. As rotas Pix pedem o cabeçalho `INTERNAL-TOKEN` (padrão `default_token`). A lista completa é a do `src/app.py`: rota que não está lá não existe, nem com uma barra a mais no fim.

### POST /pix/transaction

O contrato de entrada é o do desafio, escrito em JSON Schema em [`src/schemas/post_pix_transaction.json`](src/schemas/post_pix_transaction.json); o `$comment` de cada campo diz a medida e o porquê dela. Valores em **centavos**, datas **com fuso**, `source.geolocation` opcional, e `destination_account.participant` (o ISPB, código de 8 dígitos do banco de destino no Pix) obrigatório. O documento vale com ou sem máscara, CPF ou CNPJ. O corpo vai com `Content-Type: application/json` e com o tamanho declarado (`Content-Length`), como o curl faz sozinho.

```bash
curl -s -X POST http://localhost:3000/pix/transaction \
  -H "INTERNAL-TOKEN: default_token" \
  -H "Content-Type: application/json" \
  -d '{
    "id": "pix-000123",
    "amount": 150000,
    "transaction_date": "2026-10-17T22:15:00-03:00",
    "source_account": {
      "branch": "0001",
      "account_number": "88213",
      "opening_date": "2025-03-02T10:00:00-03:00",
      "owner": { "document_number": "123.456.789-09" }
    },
    "destination_account": {
      "participant": "60701190",
      "branch": "3675",
      "account_number": "10442",
      "opening_date": "2026-10-10T09:00:00-03:00",
      "owner": { "document_number": "987.654.321-00" }
    },
    "source": {
      "device_id": "dvc-9f2c71a8",
      "channel": "app",
      "ip": "198.51.100.23",
      "geolocation": { "latitude": -23.56, "longitude": -46.65 }
    }
  }'
```

Num banco novo, a resposta inteira, com os 22 sinais (um por linha aqui, para caber na tela):

```json
{
  "key": "b1f7f4da-ee18-403d-903d-9cd63c41fa60",
  "status": "approved",
  "metadata": {
    "policy_version": "v0.5.0",
    "decision_rule": "D-OK",
    "reasons": ["Nenhum indício pesou o bastante para pedir 2FA (risco do pagador 8, do destino 0)"],
    "scores": {"payer": 8, "recipient": 0},
    "evidence_sufficiency": {"payer": "unavailable", "recipient": "unavailable"},
    "signals": [
      {"code": "P01", "layer": 1, "granularity": "source_account", "family": "Perfil", "axis": "payer", "status": "not_triggered", "points": 0},
      {"code": "L01", "layer": 1, "granularity": "transaction", "family": "Limite", "axis": "payer", "status": "triggered", "points": 8, "evidence": {"amount": 150000, "local_time": "22:15", "amount_threshold": 100000}},
      {"code": "R01", "layer": 1, "granularity": "destination_account", "family": "Contraparte", "axis": "recipient", "status": "not_triggered", "points": 0},
      {"code": "REL03", "layer": 1, "granularity": "pair", "family": "Contraparte", "axis": "recipient", "status": "not_triggered", "points": 0},
      {"code": "L02", "layer": 2, "granularity": "payer", "family": "Limite", "axis": "payer", "status": "unavailable", "points": 0},
      {"code": "P03", "layer": 2, "granularity": "payer", "family": "Perfil", "axis": "payer", "status": "unavailable", "points": 0},
      {"code": "P04", "layer": 2, "granularity": "payer", "family": "Velocity", "axis": "payer", "status": "unavailable", "points": 0},
      {"code": "P05", "layer": 2, "granularity": "payer", "family": "Montante", "axis": "payer", "status": "unavailable", "points": 0},
      {"code": "P06", "layer": 2, "granularity": "payer", "family": "Perfil", "axis": "payer", "status": "unavailable", "points": 0},
      {"code": "P07", "layer": 2, "granularity": "payer", "family": "Velocity", "axis": "payer", "status": "unavailable", "points": 0},
      {"code": "P08", "layer": 2, "granularity": "device", "family": "Perfil", "axis": "payer", "status": "unavailable", "points": 0},
      {"code": "P09", "layer": 2, "granularity": "payer", "family": "Perfil", "axis": "payer", "status": "unavailable", "points": 0},
      {"code": "P10", "layer": 2, "granularity": "payer", "family": "Perfil", "axis": "payer", "status": "unavailable", "points": 0},
      {"code": "R02", "layer": 2, "granularity": "recipient", "family": "Velocity", "axis": "recipient", "status": "unavailable", "points": 0},
      {"code": "R03", "layer": 2, "granularity": "recipient", "family": "Contraparte", "axis": "recipient", "status": "unavailable", "points": 0},
      {"code": "R04", "layer": 2, "granularity": "destination_account+recipient", "family": "Contraparte", "axis": "recipient", "status": "unavailable", "points": 0},
      {"code": "REL01", "layer": 2, "granularity": "pair", "family": "Contraparte", "axis": "recipient", "status": "unavailable", "points": 0},
      {"code": "REL02", "layer": 2, "granularity": "pair", "family": "Contraparte", "axis": "recipient", "status": "unavailable", "points": 0},
      {"code": "DICT01", "layer": 3, "granularity": "recipient", "family": "Contraparte", "axis": "recipient", "status": "not_triggered", "points": 0}
    ]
  }
}
```

| Campo | O que é |
|---|---|
| `key` | o identificador da análise, um UUID gerado aqui (a coluna `pix_transaction_key`). O `id` serial da linha nunca sai |
| `status` | `approved`, `two_factor_required` ou `rejected` |
| `metadata.policy_version` | a versão dos pesos e da tabela de decisão que valiam quando o Pix chegou |
| `metadata.decision_rule` | a linha da tabela que decidiu: `D-RESTRICT-SIM`, `D-REJ-ATO`, `D-REJ-PAY`, `D-REJ-RCP`, `D-REJ-CROSS`, `D-DATA-UNAVAILABLE`, `D-ATO-1`, `D-ATO-2`, `D-PAY`, `D-RCP` ou `D-OK` |
| `metadata.reasons` | a frase dessa linha, com os números da evidência |
| `metadata.scores` | a nota de 0 a 100 do pagador (`payer`) e a do destino (`recipient`) |
| `metadata.evidence_sufficiency` | quanto histórico sustenta cada nota: `unavailable`, `low`, `medium` ou `high` |
| `metadata.signals` | os 22 sinais, na ordem do catálogo. `DICT02`/`DICT03` representam evidência antifraude do DICT suspeita/confirmada na fixture; `clear` e ausência não pontuam. |

**Mandou o mesmo Pix de novo?** `200`, com a análise que já estava gravada, e nada é contado de novo no histórico: é a idempotência, e quem garante é o `UNIQUE(external_id)` do banco. O mesmo `id` com outro conteúdo é `409`, inclusive quando os dois chegam ao mesmo tempo por pagadores diferentes: o segundo espera o primeiro gravar e responde como num reenvio. Repare que a comparação é com o payload como chegou: campos do JSON em outra ordem são o mesmo Pix, mas o CPF agora com máscara já é outro pedido.

### GET /pix/transaction/{id}

```bash
curl -s http://localhost:3000/pix/transaction/pix-000123 -H "INTERNAL-TOKEN: default_token"
```

Devolve o mesmo corpo do POST: os dois passam pelo mesmo DTO, lendo as mesmas linhas do banco (o porquê e o custo estão no [docs, seção 5](docs/como-o-projeto-e-organizado.md#5-schemas-e-dtos-o-que-entra-e-o-que-sai)).

### Os erros

Todo erro responde no mesmo formato, com um código próprio — quem integra programa em cima do código, e não do texto. Respostas reais, depois do POST acima:

```json
{"title": "Bad Request", "description": "'participant' is a required property in destination_account", "translation": "Payload Inválido", "code": "QIT000001"}
{"title": "Forbidden", "description": "Request must be internal", "translation": "Requisição precisa ser interna", "code": "QIT000002"}
{"title": "Pix Transaction not Found", "description": "Pix transaction with id pix-nunca-recebido was not found.", "translation": "O Pix com id pix-nunca-recebido não foi encontrado.", "code": "QIT001001"}
{"title": "Pix Transaction Id Conflict", "description": "Pix transaction with id pix-000123 was already received with a different payload.", "translation": "O Pix com id pix-000123 já foi recebido com outro conteúdo.", "code": "QIT001002"}
```

São, na ordem, o POST sem o `participant` do destino, o POST sem o `INTERNAL-TOKEN`, o GET de `pix-nunca-recebido` e o POST do `pix-000123` com `amount` 150001.

| HTTP | Código | Quando |
|---|---|---|
| 400 | `QIT000001` | o payload foge do schema: campo faltando ou a mais, data sem fuso ou impossível (31 de fevereiro), IP inválido, documento fora do formato |
| 400 | `QIT000001` | o corpo não serve: não é JSON (vazio, inválido, aninhado demais, bytes fora de UTF-8), o `Content-Type` não é JSON, o POST não declara o `Content-Length` (o corpo mandado em pedaços) ou passa de 16 KiB |
| 403 | `QIT000002` | faltou o `INTERNAL-TOKEN`, ou ele está errado |
| 404 | `QIT001001` | `GET` de um `id` que nunca chegou |
| 404 | `QIT000404` | a rota não existe |
| 405 | `QIT000405` | a rota existe, mas não aceita esse método |
| 409 | `QIT001002` | o mesmo `id` chegou com outro conteúdo |
| 500 | `QIT000500` | erro que ninguém nomeou. Tudo é desfeito, inclusive o registro do Pix: reenvie com o mesmo `id` |

Repare na ordem: sem o token, toda rota que não é pública responde 403, inclusive as que não existem. A porta vem antes do roteador, e o 404 e o 405 só aparecem para quem passou por ela. O teto de 16 KiB é quase quatro vezes o maior Pix válido medido (4,2 KiB), e é conferido pelo `Content-Length`, antes de ler um byte do corpo.

Os códigos que começam com `QIT000` são os do Base Service, em `src/errors/base_error.py`; os que começam com `QIT001` são deste serviço, em `src/errors/custom_errors.py`. Existe um terceiro código deste serviço, o `QIT001003` (`DictUnavailable`), que nunca chega ao cliente: o connector do DICT levanta, o controller captura, e o DICT01 sai `unavailable`. Código repetido derruba a API na subida, de propósito.

Toda resposta traz o cabeçalho `x-request-id`, o número de protocolo da requisição. É com ele que se acham as linhas dela no log: `docker compose logs api | grep <o id>`.

---

## 3. Configuração

Toda configuração entra por variável de ambiente. O padrão mora no `docker-compose.yml`; para trocar, `cp .env.example .env` e edite — o `.env` fica só na sua máquina e nunca vai para o Git.

| Variável | Padrão | O que é |
|---|---|---|
| `INTERNAL_TOKEN` | `default_token` | o valor que o cabeçalho `INTERNAL-TOKEN` precisa ter |
| `DICT_API_TIMEOUT` | `2` | quantos segundos esperar pelo DICT, por etapa: conectar e cada leitura, e não a chamada inteira. O DNS fica fora, limitado a ~2 s pelo `dns_opt` do compose. Zero, negativo ou texto barra a subida da API |
| `DICT_API_INTERNAL_TOKEN` | `default_token` | o token que a API manda ao DICT |
| `APP_ENV`, `SERVICE_NAME` | `local`, `bootcamp-api` | o ambiente, e o nome que aparece no log e no `GET /` |
| `API_PORT`, `DB_PORT`, `DICT_MOCK_PORT` | `3000`, `5432`, `1080` | as portas na sua máquina |
| `SERVER_LOCALHOST` | `0.0.0.0` | onde os testes procuram a API e o mock |

`DATABASE_URL` e `DICT_API_URL` são fixas no compose (`db:5432` e `http://dict-mock:1080`): dentro do Docker, banco e mock atendem pelo nome do serviço, e nunca por `localhost`. As linhas delas no `.env.example` servem a quem roda a API fora do Docker.

A API se recusa a subir sem `DATABASE_URL` e `INTERNAL_TOKEN`, com um `DICT_API_TIMEOUT` que não serve de prazo, com código de erro repetido ou com um schema de `src/schemas/` quebrado. Melhor falhar na hora de ligar do que na cara do cliente.

O `default_token` só vale porque isto é um projeto de estudo, sem dado de ninguém. Em sistema de verdade, segredo não tem padrão: ele falta, e a aplicação não sobe.

---

## 4. O mock da evidência de segurança do DICT

A camada 3 consulta evidências de segurança do DICT sobre o recebedor. O serviço `dict-mock` usa o endpoint legado abaixo apenas para compatibilidade da fixture:

```
GET /security-statistics/{documento, só dígitos}   →   200 {"security_statistics": 70}
```

O DICT de verdade devolve dados de chave, titularidade, estatísticas e marcações de segurança. O mock simplifica isso com uma resposta estatística compatível e fixtures de marcação. `clear` não significa legitimidade e `unavailable` não significa segurança. O mock conta como infraestrutura de execução, como o Postgres, e não como uma API nossa.

| Documento do recebedor | Responde | Para quê |
|---|---|---|
| qualquer um | `70` | o caso comum: nada contra o recebedor |
| `999.999.999-99` | `12` | demonstração: estatística desfavorável, DICT01 dispara (+32) |
| `888.888.888-88` | `70`, depois de 3 s | demonstração: passa do prazo de 2 s, o DICT01 sai `unavailable` e o Pix é decidido sem ele |

Repare que o DICT01 sozinho não pede 2FA: leva o destino a 32. Pede quando vem junto de outro indício do destino — conta de destino aberta há menos de 7 dias (R01, 44), um pagador com histórico que nunca mandou Pix para esse recebedor (REL01, 40) ou uma rajada de entradas (R02, R03).

Como os testes combinam a resposta de cada documento, as armadilhas do MockServer e como recomeçar uma demonstração (`docker compose restart dict-mock`): [`mockserver/README.md`](mockserver/README.md).

---

## 5. Os testes

```bash
uv venv --python 3.11 .venv && uv pip install -r requirements-dev.txt
source .venv/bin/activate
pytest
```

A stack precisa estar de pé (seção 1). O `tests/conftest.py` espera a API, o mock e a resposta padrão do mock, até 60 s, antes do primeiro teste; se não vierem, a suíte para com uma mensagem só, dizendo o que faltou e o comando que resolve. Os testes leem o `.env` sozinhos: trocou a porta ali, eles batem na porta nova. A suíte leva uns 12 s.

Todos são de integração. A maioria fala HTTP com a API, como um cliente faria;
os testes de resiliência também usam controller/repositories reais e sessões
PostgreSQL para simular falha de infraestrutura e expiração de lease. Cada
teste cria o próprio mundo (documentos, contas, aparelhos e IPs aleatórios),
monta o histórico com POSTs de data escolhida e não precisa limpar o banco. O
que cada arquivo prova está em [`tests/integration/pix/README.md`](tests/integration/pix/README.md); para rodar um só, `pytest -v tests/integration/pix/test_cenarios_rfc.py`.

---

## 6. O caminho de uma requisição

Cinco middlewares, de fora para dentro, atendem toda requisição: o identificador da requisição, o log de entrada e saída, o `INTERNAL-TOKEN` (sem ele, 403), o tamanho do corpo (só no POST: sem `Content-Length` ou acima de 16 KiB, 400) e a sessão de banco. Depois, o resource confere o corpo contra o schema (400) e chama o controller, que faz, nesta ordem:

```
1. converte o payload            data impossível é 400, antes de tudo
2. procura o id                  reenvio idêntico: a análise gravada (200);
                                 outro conteúdo: 409. Sem DICT, sem contar nada
3. pergunta ao DICT              pelo recebedor, ANTES de travar qualquer linha,
                                 e com a conexão de volta ao pool durante a espera
4. cria customer e account       na primeira aparição, em ordem de documento:
                                 dois Pix cruzados não esperam um pelo outro
5. trava o pagador               SELECT ... FOR NO KEY UPDATE no customer dele,
                                 e procura o id de novo
6. conta o histórico             monta o Context e roda o motor
7. grava o Pix e os 22 sinais    e só então dá o commit → 201
8. a não ser que o mesmo id      tenha chegado junto por outro pagador, que a
                                 trava não segura, e gravado primeiro: 200 ou 409
```

O histórico usa `received_at`, o recebimento do servidor; `transaction_date` é reservado ao relógio de negócio. O cadastro de devices pode deixar sinais como `unavailable`, mas só produz `D-DATA-UNAVAILABLE` quando nenhuma restrição ou rejeição comprovável puder decidir. A conclusão do recibo usa `owner_token` em uma atualização condicional: um avaliador que perdeu o lease não pode completar ou marcar como falho o recibo de outro.

Quem fala com o banco, o DICT e o motor é o controller, e só ele. O mapa das pastas, o que cada uma pode e não pode e onde mexer para cada mudança estão em [`docs/como-o-projeto-e-organizado.md`](docs/como-o-projeto-e-organizado.md).

---

## 7. Quando dá errado

**Sem o uv.** Com o Python 3.11 da máquina: `python3.11 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt`, e depois `source .venv/bin/activate` e `pytest`.

**`port is already allocated`.** Outro programa usa a porta (é comum um Postgres instalado ocupando a 5432). Escolha outras, na subida e nos testes — e na linha que espera o mock:

```bash
API_PORT=3001 DB_PORT=5433 DICT_MOCK_PORT=1081 docker compose up -d --build --wait
API_PORT=3001 DICT_MOCK_PORT=1081 pytest
```

Ou ponha as três no `.env`, que a subida e os testes leem.

**`relation "..." does not exist`, `column "..." does not exist` ou `there is no unique or exclusion constraint matching the ON CONFLICT specification`.** O `database.sql` mudou e o banco não ficou sabendo: o arquivo só roda quando o banco nasce. `docker compose down -v` e suba de novo. O `-v` leva junto todo Pix gravado até aqui.

**Mexeu no `requirements.txt`.** As dependências da API moram na imagem, e o `--reload` só recarrega o `src/`. Reconstrua: `docker compose up -d --build --wait`. As dos testes moram no seu `.venv`: `uv pip install -r requirements-dev.txt` de novo.

**Todo Pix sai com o DICT01 `unavailable`.** O mock ainda está subindo (até 18 s); alguém mandou um `PUT /mockserver/reset`, que apaga também as expectativas do arquivo; ou o mock passou de 5.000 expectativas e descartou a padrão ([`mockserver/README.md`](mockserver/README.md)). `docker compose restart dict-mock` traz tudo de volta.

**A suíte parou com "A stack não ficou de pé".** A API ou o mock não responderam em 60 s nas portas que o teste procurou. Suba a stack e confira se as portas dos testes são as mesmas da subida.

---

## 8. Números

Medidos na máquina de desenvolvimento (8 núcleos), com a stack do compose (`--reload`, log em DEBUG). Servem como ordem de grandeza; não são desempenho de produção.

| O quê | Quanto |
|---|---|
| POST de um Pix novo, mediana (p50) | 16 a 19 ms |
| Reenvio idêntico / GET | ~5 ms / ~4 ms |
| Motor (três camadas, tetos e tabela) | ~0,1 ms |
| Histórico (as 4 consultas) | ~2 a 3 ms no caso típico; ~15 ms num pagador com 4 mil Pix |
| Um processo da API | ~85 a 100 Pix/s |
| 4 processos (`--workers 4`) | ~220 a 250 Pix/s |
| Mesmo pagador, com 8 clientes | ~60 Pix/s, um de cada vez, sem atrasar os outros pagadores |
| Suíte de testes | 189 testes em ~12 s |
| Efeito da política v0.5.0 (DICT01 com teto próprio) na taxa de 2FA | estimativa ilustrativa, sem dado real; não deve ser apresentada como métrica validada |

- **Onde trava:** na CPU de um processo Python; o banco fica com folga. A API não guarda estado (trava, idempotência e histórico estão no Postgres), e escala com mais processos ou réplicas, cada um com o seu pool de conexões.
- **DICT lento custa vazão, e não decisão:** cada Pix espera o prazo numa das 40 threads do processo, e a vazão cai para ~12 a 18 Pix/s; a decisão sai, com o DICT01 `unavailable`.
- **O histórico do pagador cresce:** a consulta lê todos os Pix do documento, porque a mediana precisa deles. A do recebedor já tem teto: uma loja com 50 mil Pix recebidos passou de 37 para 17 ms por POST.
- **Disco:** ~77 GB por mês a 20 milhões de Pix (estimativa), dois terços na tabela de sinais.

Próximos passos: `--workers` no lugar do `--reload` fora do desenvolvimento; `pool_size` maior por processo; pular as consultas de quem acabou de ser criado; um perfil do pagador pré-calculado (mediana, aparelhos e IPs conhecidos); não gravar os `not_triggered`, cerca de 70% da tabela de sinais.

---

## 9. Limites de engenharia

Os de antifraude estão no [README da raiz](../README.md#limites-conhecidos). Os daqui:

- **CNPJ alfanumérico:** o novo formato da Receita ainda não é aceito; pede mudar junto o schema, o CHECK de `customer` e a normalização do documento.
- **Corridas aceitas:** a trava segura um pagador por vez. Dois pagadores no mesmo aparelho ou no mesmo recebedor, no mesmo instante, contam o histórico sem o Pix um do outro: um sinal de rajada pode chegar um Pix atrasado.
- **Idempotência pelo payload cru e reenvio depois de um 500**, descritos na seção 2: o mesmo Pix com o CPF agora mascarado volta 409, e o Pix que deu 500 não fica no histórico.

---

## 10. A licença

**MIT**, como o Base Service: pode usar, copiar e modificar, mantendo o arquivo [`LICENSE`](LICENSE) junto.
