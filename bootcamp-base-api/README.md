# Antifraude Pix — a API

A API do motor antifraude Pix: recebe um Pix em `POST /pix/transaction`, decide entre `approved`, `two_factor_required` e `rejected`, grava o Pix com a decisão explicada e devolve a análise. `GET /pix/transaction/{id}` devolve o que foi gravado. É **Python 3.11 + FastAPI**, com **PostgreSQL 16**, rodando em **Docker**, e nasceu do Base Service do bootcamp. Para decidir, ela consulta o DICT, o diretório do Banco Central que liga chave Pix a conta; aqui quem faz o papel dele é um mock, um servidor de mentira que devolve as respostas combinadas com ele (seção 4).

As decisões seguem a política `v0.6.0` ([`src/antifraud/policy.py`](src/antifraud/policy.py)), que parte da [RFC v0.5 do Notion](https://app.notion.com/p/RFC-v0-5-Modelagem-de-Risco-Antifraude-Pix-1-3fc6b8cbe2cc8022aeeaf6e39530a099) com os ajustes listados no cabeçalho do arquivo. O DICT responde, numa consulta só, a estatística de segurança do recebedor (`DICT01`) e a marcação de fraude dele (`DICT02`, `DICT03`): sem marcação é informação, e DICT fora do ar deixa a camada 3 `unavailable` — nunca segurança.

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
| `db` | o PostgreSQL 16, com as tabelas do [`database/database.sql`](database/database.sql) e `shm_size` de 256 MB (nos 64 MB padrão do Docker o `VACUUM` paralelo já não cabe) | 5432 |
| `dict-mock` | o DICT de mentira, um MockServer (seção 4) | 1080 |

O `--wait` devolve o terminal quando o `/health_check` da API responde e o banco aceita conexão. O mock conta como pronto assim que o container roda: a imagem dele não tem shell para um healthcheck, e ele ainda leva de 2 a 18 s para atender (medido). É isso que a segunda linha espera; até lá, a camada 3 sai `unavailable` e o Pix é decidido pelas outras duas.

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

Num banco novo, a resposta inteira, com os 23 sinais (um por linha aqui, para caber na tela):

```json
{
  "key": "b3ed101b-45ee-4988-81d6-229e1580e4a2",
  "status": "approved",
  "metadata": {
    "policy_version": "v0.6.0",
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
      {"code": "L03", "layer": 2, "granularity": "payer", "family": "Limite", "axis": "payer", "status": "not_triggered", "points": 0},
      {"code": "G01", "layer": 2, "granularity": "payer", "family": "Localização", "axis": "payer", "status": "unavailable", "points": 0},
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
      {"code": "DICT01", "layer": 3, "granularity": "recipient", "family": "Contraparte", "axis": "recipient", "status": "not_triggered", "points": 0},
      {"code": "DICT02", "layer": 3, "granularity": "recipient", "family": "Contraparte", "axis": "recipient", "status": "not_triggered", "points": 0},
      {"code": "DICT03", "layer": 3, "granularity": "recipient", "family": "Contraparte", "axis": "recipient", "status": "not_triggered", "points": 0}
    ]
  }
}
```

Os 8 são do L01 (22h15 com R$ 1.500); a conta de destino tem 7 dias e 13 horas, e o R01 fica calado; os 15 sinais de histórico saem `unavailable` porque é o primeiro Pix do pagador, e o L03 fica calado porque R$ 1.500 está longe do teto de R$ 5.000 de quem nunca teve Pix aprovado. O DICT respondeu o padrão do mock (estatística 70, sem marcação): DICT01, DICT02 e DICT03 calados.

| Campo | O que é |
|---|---|
| `key` | o identificador da análise, um UUID gerado aqui (a coluna `pix_transaction_key`). O `id` serial da linha nunca sai |
| `status` | `approved`, `two_factor_required` ou `rejected` |
| `metadata.policy_version` | a versão dos pesos e da tabela de decisão que valiam quando o Pix chegou |
| `metadata.decision_rule` | a linha da tabela que decidiu, na ordem em que são tentadas: `D-REJ-ATO`, `D-REJ-RCP`, `D-REJ-CROSS`, `D-ATO-1`, `D-ATO-2`, `D-PAY`, `D-LIMIT`, `D-SCAM`, `D-DRAIN`, `D-RCP` ou `D-OK` |
| `metadata.reasons` | a frase dessa linha, com os números da evidência |
| `metadata.scores` | a nota de 0 a 100 do pagador (`payer`) e a do destino (`recipient`) |
| `metadata.evidence_sufficiency` | quanto histórico sustenta cada nota: `unavailable`, `low`, `medium` ou `high` |
| `metadata.signals` | os 23 sinais, na ordem do catálogo, cada um com camada, família, eixo, granularidade (a chave que a regra consultou), estado e pontos; `evidence` só quando disparou |

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

## 4. O mock do DICT

A camada 3 pergunta ao DICT pelo documento do recebedor, uma vez por Pix, e a resposta traz duas coisas: a estatística de segurança e a marcação de fraude. Quem responde aqui é o serviço `dict-mock`:

```
GET /security-statistics/{documento, só dígitos}   →   200 {"security_statistics": 70, "fraud_marker": null}
```

`security_statistics` é um inteiro de 0 a 100, e abaixo de 30 dispara o DICT01 (+32). `fraud_marker` é `null` (ou ausente) quando não há marcação, `"suspicious"` (DICT02, +20) ou `"confirmed"` (DICT03, +25); a marcação do recebedor é também a porteira das rejeições `D-REJ-RCP` e `D-REJ-CROSS`. O DICT de verdade devolve contadores de fraude por janela e as marcações que os participantes registram contra o documento (Manual do DICT, caps. 10 e 18); o mock reduz isso a um número e uma palavra. Qualquer resposta fora desse combinado — erro, prazo estourado, estatística fora da faixa, marcação desconhecida — deixa os três sinais `unavailable`, e indisponibilidade nunca vira segurança. O mock conta como infraestrutura de execução, como o Postgres, e não como uma API nossa.

| Documento do recebedor | Responde | Para quê |
|---|---|---|
| qualquer um | `70`, sem marcação | o caso comum: nada contra o recebedor |
| `999.999.999-99` | `12`, marcação `confirmed` | demonstração: DICT01 +32 e DICT03 +25. Um primeiro Pix pra ele já pede 2FA (destino 57, `D-RCP`); com rajada de muita gente, é rejeição (`D-REJ-RCP`) |
| `777.777.777-77` | `70`, marcação `suspicious` | demonstração: só o DICT02 (+20), que sozinho aprova, mas abre a porteira das rejeições do destino |
| `888.888.888-88` | `70`, depois de 3 s | demonstração: passa do prazo de 2 s, a camada 3 sai `unavailable` e o Pix é decidido sem ela |

Repare que o DICT01 sozinho não pede 2FA: leva o destino a 32. Pede quando vem junto de outro indício do destino — conta de destino aberta há menos de 7 dias (R01, 44), um pagador com histórico que nunca mandou Pix para esse recebedor (REL01, 40), a marcação (DICT02 ou DICT03) ou uma rajada de entradas (R02, R03).

Como os testes combinam a resposta de cada documento, as armadilhas do MockServer e como recomeçar uma demonstração (`docker compose restart dict-mock`): [`mockserver/README.md`](mockserver/README.md).

---

## 5. Os testes

```bash
uv venv --python 3.11 .venv && uv pip install -r requirements-dev.txt
source .venv/bin/activate
pytest
```

A stack precisa estar de pé (seção 1). O `tests/conftest.py` espera a API, o mock e a resposta padrão do mock, até 60 s, antes do primeiro teste; se não vierem, a suíte para com uma mensagem só, dizendo o que faltou e o comando que resolve. Os testes leem o `.env` sozinhos: trocou a porta ali, eles batem na porta nova. São 198 testes, em uns 20 s.

Todos são de integração, e só HTTP: nenhum importa `src/`, nenhum grava nada por SQL. Cada teste cria o próprio mundo (documentos, contas, aparelhos e IPs sorteados), monta o histórico com POSTs de data escolhida — as janelas contam em `transaction_date`, então "10 Pix em 10 dias" é mandar 10 Pix com essas datas — e escreve no mock o roteiro do recebedor dele quando o assunto é a camada 3. Não precisa limpar o banco entre rodadas. O que cada arquivo prova está em [`tests/integration/pix/README.md`](tests/integration/pix/README.md); para rodar um só, `pytest -v tests/integration/pix/test_cenarios_rfc.py`.

---

## 6. O caminho de uma requisição

Cinco middlewares, de fora para dentro, atendem toda requisição: o identificador da requisição, o log de entrada e saída, o `INTERNAL-TOKEN` (sem ele, 403), o tamanho do corpo (só no POST: sem `Content-Length` ou acima de 16 KiB, 400) e a sessão de banco. Depois, o resource confere o corpo contra o schema (400) e chama o controller, que faz, nesta ordem:

```
1. converte o payload            data impossível é 400, antes de tudo
2. procura o id                  reenvio idêntico: a análise gravada (200);
                                 outro conteúdo: 409. Sem DICT, sem contar nada
3. pergunta ao DICT              pelo recebedor, ANTES de travar qualquer coisa,
                                 e com a conexão de volta ao pool durante a espera
4. trava o pagador               advisory lock pelo documento, até o commit:
                                 dois Pix do mesmo CPF passam um de cada vez
5. cria customer e account       na primeira aparição, em ordem de documento:
                                 dois Pix cruzados não esperam um pelo outro
6. procura o id de novo          outro POST do mesmo Pix pode ter gravado
                                 enquanto este esperava a trava
7. conta o histórico             por transaction_date, nas contas do documento;
                                 monta o Context e roda o motor
8. grava o Pix e os 23 sinais    INSERT ... ON CONFLICT (external_id) DO NOTHING,
                                 e só então dá o commit → 201
9. a não ser que o mesmo id      tenha chegado junto por outro pagador, que a
                                 trava não segura, e gravado primeiro: 200 ou 409
```

O histórico conta em `transaction_date`, o tempo do negócio, que vem do sistema do banco no payload: é o que deixa qualquer cenário ser reproduzido mandando Pix com as datas escolhidas, e um Pix que chegue atrasado cai no lugar certo. O `received_at` fica gravado só para auditoria.

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

**Todo Pix sai com a camada 3 `unavailable`.** O mock ainda está subindo (até 18 s); alguém mandou um `PUT /mockserver/reset`, que apaga também as expectativas do arquivo; ou o mock passou de 5.000 expectativas e descartou a padrão ([`mockserver/README.md`](mockserver/README.md)). `docker compose restart dict-mock` traz tudo de volta.

**A suíte parou com "A stack não ficou de pé".** A API ou o mock não responderam em 60 s nas portas que o teste procurou. Suba a stack e confira se as portas dos testes são as mesmas da subida.

---

## 8. Números

Medidos na máquina de desenvolvimento (8 núcleos), com a stack do compose como ela sobe: um processo, `--reload`, log em DEBUG, num banco com 160 mil Pix. Servem como ordem de grandeza; não são desempenho de produção.

| O quê | Quanto |
|---|---|
| POST de um Pix novo, mediana (p50), um cliente | ~21 ms (p90 24 ms) |
| Reenvio idêntico / GET | ~5 ms / ~4 ms |
| Consulta do histórico do pagador, num pagador com 5 mil Pix | ~12 ms, sem voltar à tabela (index-only scan no índice cobrindo) |
| Consulta do par pagador→recebedor, o mesmo pagador | ~0,1 ms (15 páginas) |
| Um processo da API, 8 clientes | ~65 Pix/s, p50 123 ms |
| Um processo da API, 16 clientes | ~62 Pix/s, p50 256 ms |
| Suíte de testes | 198 testes em ~19 s |
| Disco | ~5 KB por Pix: ~1,5 KB em `pix_transaction` com os índices, mais 23 sinais de ~150 B. A 20 milhões de Pix por mês, uns 100 GB |
| WAL | ~16 KB por Pix |

- **Onde trava:** na CPU de um processo Python; o banco fica com folga. A API não guarda estado (trava, idempotência e histórico estão no Postgres), e escala com mais processos ou réplicas, cada um com o seu pool de conexões — veja o limite do `--reload` na seção 9.
- **DICT lento custa vazão, e não decisão:** cada Pix espera o prazo de 2 s numa das 40 threads do processo, o que dá no máximo 20 Pix/s por processo enquanto o DICT não responde; a decisão sai, com a camada 3 `unavailable`.
- **O histórico do pagador cresce:** a consulta lê todos os Pix do documento, porque a mediana, os dias ativos e o aparelho e o IP conhecidos precisam deles; o índice cobrindo evita a volta à tabela, mas o custo continua linear no histórico. A do recebedor tem teto, e um destino com 50 mil Pix custa o mesmo que um inédito.
- **Sinais vazios:** 98% das linhas de `pix_transaction_rule_result` são `not_triggered` ou `unavailable`, com evidência `{}`; só o estado informa.

Próximos passos: `--workers` no lugar do `--reload` fora do desenvolvimento; janela de 90 dias (ou um perfil pré-calculado) para a mediana e os aparelhos e IPs conhecidos; não gravar os sinais vazios e devolvê-los no GET a partir do catálogo da `policy_version`; partição mensal de `pix_transaction` para a retenção.

---

## 9. Limites de engenharia

Os de antifraude estão no [README da raiz](../README.md#limites-conhecidos). Os daqui:

- **Um processo só:** o compose sobe a API com `--reload`, que recarrega o `src/` a cada mudança e, por isso, não aceita `--workers`. Fora do desenvolvimento, o comando do `Dockerfile` (sem `--reload`) com `--workers N`; cada worker abre até 15 conexões (`pool_size` 5 mais 10 de folga), e o Postgres aceita 100: até 6 workers por banco sem um pgbouncer na frente.
- **Corridas aceitas:** a trava segura um pagador por vez, e só ele. Dois pagadores no mesmo recebedor ou no mesmo aparelho, no mesmo instante, contam o histórico sem o Pix um do outro: R02, R03 e P08 podem chegar um Pix atrasados. Travar também o recebedor poria todos os pagadores de uma loja em fila única.
- **O histórico do pagador não tem janela:** o custo da consulta cresce com a vida do documento (seção 8).
- **Threads e conexões:** 40 threads por processo e 15 conexões; a 41ª requisição espera uma thread, e quem não consegue conexão em 5 s, ou não termina um comando em 10 s, falha com nome em vez de pendurar.
- **DICT sem disjuntor:** com o DICT fora do ar, cada Pix paga os 2 s do prazo; não há "pular o DICT por um tempo depois de N falhas".
- **CNPJ alfanumérico:** o novo formato da Receita ainda não é aceito; pede mudar junto o schema, o CHECK de `customer` e a normalização do documento.
- **Idempotência pelo payload cru e reenvio depois de um 500**, descritos na seção 2: o mesmo Pix com o CPF agora mascarado volta 409, e o Pix que deu 500 não fica no histórico.

---

## 10. A licença

**MIT**, como o Base Service: pode usar, copiar e modificar, mantendo o arquivo [`LICENSE`](LICENSE) junto.
