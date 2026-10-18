# Testes de integração do Pix

Tudo pela porta da frente: os testes falam HTTP com a API de pé, montam o histórico de cada cenário com POSTs de data escolhida, escrevem o roteiro do mock do DICT quando o assunto é a camada 3, e conferem a decisão. Nenhum teste importa `src/`, nenhum grava nada por SQL, nenhum é unitário: o que erra em silêncio num antifraude é a contagem do histórico e a montagem do `Context`, e só o caminho inteiro — HTTP, banco, motor, banco, HTTP — pega isso.

## Como rodar

Da pasta `bootcamp-base-api/`, com o [uv](https://docs.astral.sh/uv/) e o Python 3.11:

```bash
uv venv --python 3.11 .venv
uv pip install -r requirements-dev.txt
docker compose up -d --build --wait
.venv/bin/pytest
```

São 198 testes, em uns 20 s: os cenários da RFC, os dois lados de cada limiar, o histórico, a camada 3, as rejeições, o contrato, a concorrência e o G01. O mais lento espera de propósito o prazo do DICT. Se a stack não estiver de pé, a suíte para no começo com uma mensagem só, e o comando que resolve. Mudou o `database/database.sql`: `docker compose down -v` antes de subir.

Cada teste cria as pessoas dele — documentos, contas, aparelhos e IPs sorteados —, e por isso a suíte roda de novo sem limpar o banco, e roda em paralelo com quem mais estiver usando a API.

## Os arquivos

| Arquivo | O que prova |
|---|---|
| `test_cenarios_rfc.py` | os 17 cenários de aceitação da RFC (o 15 mora no contrato e na concorrência), com a conta de cada um no docstring |
| `test_fronteiras.py` | os dois lados de cada limiar (P01, L01, L02, L03, P04, P05, P06, P07, P08, R02, R03, REL02), os tetos e o piso dos grupos, D-PAY e D-RCP em 39 e 40, a pré-condição de 3 Pix aprovados de aparelho e IP, D-SCAM e D-DRAIN com o caso que casa e o que não casa, e os degraus da suficiência |
| `test_historico.py` | o que entra no histórico: só Pix aprovado ensina aparelho, IP, mediana e relação; a rajada soma toda tentativa; conta conjunta é um histórico por titular; conta de destino e titular são identidades diferentes; Pix com data posterior não entra; IPv6 escrito de outro jeito é o mesmo IP |
| `test_camada_dict.py` | a camada 3: a fronteira do DICT01 em 29/30, a marcação de fraude virando DICT02/DICT03, marcação fora do combinado, mock lento, com erro ou com corpo torto (tudo `unavailable`), quem é consultado e que o reenvio não consulta de novo |
| `test_rejeicoes.py` | as três rejeições (D-REJ-ATO, D-REJ-RCP, D-REJ-CROSS) e o negativo de cada uma, que fica em 2FA |
| `test_contrato.py` | 201, 200, 400, 403, 404 e 409, o corpo da resposta e os 23 sinais do catálogo, GET igual ao POST nos mesmos bytes, 27 payloads fora do contrato e 8 variações dentro dele, corpo que nem é JSON, o teto de 16 KiB, documento com e sem pontuação |
| `test_concorrencia.py` | POSTs ao mesmo tempo: a trava por pagador, o mesmo id em corrida (200 ou 409, nunca 500) e Pix cruzados de gente nova |
| `test_geolocalizacao.py` | G01: sem padrão formado, viagem distante, dois centros habituais, abaixo de 300 km e o corte de 24 h |
| `../test_healthcheck.py` | as rotas públicas, o 403 sem token, o 404 e o 405 do Base Service |

Os nomes dos cenários são os da tabela da RFC §9. O cenário 15 está dividido: `test_reenvio_devolve_mesma_analise` e `test_mesmo_id_payload_diferente_409` em `test_contrato.py`, e `test_concorrencia_mesmo_pagador_conta_uma_vez` em `test_concorrencia.py`.

## As rejeições

Rejeitar é a decisão cara, e a tabela só rejeita em dois casos: a tomada de conta com todos os sinais juntos, ou um destino que o DICT já marcou como fraude. `test_rejeicoes.py` monta cada caso e, ao lado, a mesma história sem a peça que rejeita:

| Cenário | Preparação | Resultado |
|---|---|---|
| tomada de conta completa | 10 Pix de R$ 1.000 no destino de sempre; depois 5 de R$ 6.000 em 5 min, de aparelho novo, cada um pra um destino inédito (P03, P04, P05, P06, P07, L02; pagador 80) | `D-REJ-ATO`, `rejected` |
| a mesma rajada pro destino de sempre | sem o P07; pagador 71 | `D-ATO-1`, 2FA — nota alta sem a combinação não rejeita |
| destino marcado recebendo rajada | 5 pagadores em 5 min num titular com estatística 12 e marcação `suspicious` ou `confirmed` no mock (R02, R03, DICT01, DICT02 ou DICT03; destino 96 ou 100) | `D-REJ-RCP`, `rejected` |
| a mesma rajada sem marcação | estatística 12 e `fraud_marker: null`, ou DICT fora do ar (destino 76 ou 44) | `D-RCP`, 2FA — falta de resposta não é marcação |
| os dois eixos no limiar e o destino marcado | rajada pra destinos inéditos (pagador 43) num destino com estatística 12 e marcação `confirmed` (destino 65) | `D-REJ-CROSS`, `rejected` |
| os dois eixos no limiar, sem marcação | o mesmo, com `fraud_marker: null` (destino 40) | `D-PAY`, 2FA |

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

A conta do docstring é a que a banca refaria no papel, e os asserts cobram ela inteira: status, linha da tabela, sinais disparados e as duas notas. As sementes do histórico passam pelo `seed`, que derruba o teste na primeira que não terminar como o cenário precisa — um histórico montado errado mediria outra coisa sem avisar.

## Os helpers

Ficam em `tests/utils/`:

- `clock.py` — o relógio dos cenários: `T0` (14/10/2026, 14h em São Paulo) e as funções que espalham Pix por minutos e dias antes dele. O motor conta tudo em `transaction_date`, e por isso nenhum teste espera relógio nem depende de que horas são agora.
- `identities.py` — as pessoas sorteadas: CPF e CNPJ com dígito verificador, contas, ISPB, aparelho e IP. Os documentos de demonstração do mock (dígitos repetidos) nunca saem do sorteio.
- `pix.py` — o Pix pela porta da frente: `pix_payload`, `post_pix`, `get_pix`, `post_raw` (bytes crus, pros testes do corpo) e `seed`/`seed_pix`, que mandam o histórico conferindo cada semente.
- `dict_mock.py` — o roteiro do mock do DICT por documento (estatística, marcação, atraso, erro, corpo torto) e o `verify` de quem foi consultado; entra pela fixture `dict_mock` de `tests/conftest.py`, que apaga o roteiro no fim do teste.
- `requisition.py` — o cliente HTTP, com prazo por requisição e a mensagem de "API fora do ar".
