# Testes de integração do Pix

Pela porta da frente: cada teste fala HTTP com a API de pé, monta o histórico com POSTs de data escolhida e confere a decisão. Nada aqui importa `src/`, e nenhum teste roda sem a stack.

## Como rodar

Da pasta `bootcamp-base-api/`, com o [uv](https://docs.astral.sh/uv/) e o Python 3.11:

```bash
uv venv --python 3.11 .venv
uv pip install -r requirements-dev.txt
docker compose up -d --build --wait
.venv/bin/pytest
```

São 168 testes (162 do Pix e 6 do health check), em uns 12 s. O mais lento espera de propósito os 2 s do DICT lento. Se a stack não estiver de pé, a suíte para no começo com uma mensagem só, e o comando que resolve. Mudou o `database/database.sql`: `docker compose down -v` antes de subir.

Cada teste cria as pessoas dele — documentos, contas, aparelhos e IPs sorteados —, e por isso a suíte roda de novo sem limpar o banco.

## Os arquivos

| Arquivo | O que prova |
|---|---|
| `test_cenarios_rfc.py` | os cenários de aceitação da RFC v0.3 (§9), com a conta de cada um no docstring |
| `test_fronteiras.py` | os dois lados de cada limiar, os tetos e o piso dos grupos, D-PAY e D-RCP em 39 e 40, a pré-condição de 3 Pix aprovados e a suficiência |
| `test_historico.py` | o que entra no histórico: só Pix aprovado ensina aparelho e mediana, a rajada soma toda tentativa, conta conjunta é um histórico por titular, conta de destino e titular são identidades diferentes, Pix com data posterior não entra |
| `test_camada_dict.py` | a camada 3: DICT01, o 2FA de conta nova mal avaliada, mock lento, com erro ou torto, e quem é consultado |
| `test_contrato.py` | 201, 200, 400, 403, 404 e 409, o corpo da resposta, GET igual ao POST nos mesmos bytes, corpo que nem é JSON, documento com e sem pontuação |
| `test_concorrencia.py` | POSTs ao mesmo tempo: a trava por pagador, o mesmo id em corrida (200 ou 409, nunca 500) e Pix cruzados de gente nova |

Os nomes dos cenários são os da tabela da RFC §9. O cenário 15 está dividido: `test_reenvio_devolve_mesma_analise` e `test_mesmo_id_payload_diferente_409` em `test_contrato.py`, e `test_concorrencia_mesmo_pagador_conta_uma_vez` em `test_concorrencia.py`.

## Como ler a conta dos docstrings

```
P03 +18        o sinal P03 disparou e somou 18 pontos
P04 calado     o sinal rodou e não disparou (not_triggered)
unavailable    o sinal não tinha histórico pra responder, e não soma nada
teto 25        o grupo de teto soma no máximo isso
pagador 46     a nota do eixo, em metadata.scores.payer
D-ATO-1        a linha da tabela de decisão que casou primeiro
```

Grupo de teto não é família. A família sai na resposta (Velocity, Montante, Perfil...); o grupo de teto é o do `CAPS` de `src/antifraud/policy.py` e junta os sinais que olham o mesmo padrão. O `payer_velocity`, por exemplo, junta o P04 (família Velocity) e o P05 (família Montante).

## Os helpers

Ficam em `tests/utils/`: `clock.py` (o tempo do negócio), `identities.py` (as pessoas sorteadas), `pix.py` (payload, cliente e sementes) e `dict_mock.py` (o roteiro do mock do DICT, pela fixture `dict_mock` de `tests/conftest.py`).
