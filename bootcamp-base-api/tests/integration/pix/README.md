# Testes de integração do Pix

Pela porta da frente na maioria dos casos: os testes falam HTTP com a API de pé,
montam histórico com POSTs de data escolhida e conferem a decisão. Os dois
testes de resiliência usam também controller/repositories reais e duas sessões
PostgreSQL para simular falhas e expiração de lease; nenhum é unitário isolado.

## Como rodar

Da pasta `bootcamp-base-api/`, com o [uv](https://docs.astral.sh/uv/) e o Python 3.11:

```bash
uv venv --python 3.11 .venv
uv pip install -r requirements-dev.txt
APP_ENV=test docker compose up -d --build --wait
.venv/bin/pytest
```

A suíte cobre 195 testes: os 18 cenários principais da RFC, regressões,
fronteiras, contrato, concorrência e cenários adicionais da política v0.5. O mais lento espera
de propósito o timeout do DICT. Se a stack não estiver de pé, a suíte para no
começo com uma mensagem só, e o comando que resolve. Mudou o
`database/database.sql`: `docker compose down -v` antes de subir.

Cada teste cria as pessoas dele — documentos, contas, aparelhos e IPs sorteados —, e por isso a suíte roda de novo sem limpar o banco.

## Os arquivos

| Arquivo | O que prova |
|---|---|
| `test_cenarios_rfc.py` | os cenários de aceitação da RFC v0.5, com a conta de cada um no docstring |
| `test_fronteiras.py` | os dois lados de cada limiar, os tetos e o piso dos grupos, D-PAY e D-RCP em 39 e 40, a pré-condição de 3 Pix aprovados e a suficiência |
| `test_historico.py` | o que entra no histórico: só Pix aprovado ensina aparelho e mediana, a rajada soma toda tentativa, conta conjunta é um histórico por titular, conta de destino e titular são identidades diferentes, e relógio injetado não apaga histórico concluído |
| `test_camada_dict.py` | a camada 3: DICT01, indisponibilidade, mock lento/com erro/corpo inválido e quem é consultado |
| `test_politica_v05.py` | restrição prioritária e as quatro rejeições demonstrativas da RFC v0.5 com evidências DICT seedadas |
| `test_contrato.py` | 201, 200, 400, 403, 404 e 409, o corpo da resposta, GET igual ao POST nos mesmos bytes, corpo que nem é JSON, documento com e sem pontuação |
| `test_concorrencia.py` | POSTs ao mesmo tempo: a trava por pagador, inversão entre chegada e avaliação, o mesmo id em corrida (200 ou 409, nunca 500) e Pix cruzados de gente nova |
| `test_resiliencia.py` | Restrição ativa vence indisponibilidade do cadastro de devices; owner expirado não sobrescreve receipt reassumido; GET distingue receipt em processamento e falho |
| `test_geolocalizacao.py` | G01: cobertura insuficiente, viagem distante, múltiplos centros habituais, limiar de 300 km, janela de 24h e combinações de 2FA/rejeição |

Os nomes dos cenários são os da tabela da RFC §9. O cenário 15 está dividido: `test_reenvio_devolve_mesma_analise` e `test_mesmo_id_payload_diferente_409` em `test_contrato.py`, e `test_concorrencia_mesmo_pagador_conta_uma_vez` em `test_concorrencia.py`.

## Formato declarativo

`tests/utils/scenario.py` define o formato de um cenário novo. Ele separa
`authoritative` (cadastros externos), `history` (Pix observados), `derived`
(baseline que a aplicação deve calcular), `request` (Pix avaliado) e
`expected` (oráculo independente). O runner envia cada item de `history` pela
API, nunca faz INSERT direto no banco, e depois compara o POST com o GET.

Os seeds de `authoritative.devices`, `authoritative.restrictions` e
`authoritative.dict_security` gravam fontes autoritativas separadas; por
compatibilidade, o runner ainda aceita o nome antigo `authoritative.external_risk`; eles não
criam Pix nem transformam uma aprovação em confiança. Assim, o banco recebe
eventos de entrada, cadastros explícitos de fonte e a análise persistida; os
resultados esperados ficam no teste. Uma execução válida prova os dois
comportamentos: a decisão foi calculada e o mesmo resultado foi gravado.

## Cenários adicionais da política v0.5

Além dos 18 cenários da RFC, `test_politica_v05.py` documenta as combinações
que podem rejeitar e os negativos que devem permanecer em 2FA:

| Cenário | Preparação | Resultado |
|---|---|---|
| ATO completo | P03 + P04 + P06 + P07, score do pagador ≥70 | `D-REJ-ATO` |
| ATO incompleto | P03 + P06, sem P07 | `D-ATO-1` |
| PAY com evidência DICT | P04 + P06 + P07, score ≥70, evidência `suspicious` no pagador | `D-REJ-PAY` |
| Restrição ativa | Restrição vigente no pagador | `D-RESTRICT-SIM` |
| Restrição temporal | Restrição futura ou expirada | `D-OK` |
| RCP com evidência DICT | R02 + R03, DICT desfavorável, score do destino ≥55, evidência `suspicious` | `D-REJ-RCP` |
| RCP sem evidência adversa | Mesmo caso com fonte `clear` ou `unavailable` | `D-RCP` / 2FA |
| CROSS | Risco do pagador e destino ≥40, evidência `confirmed_in_mock` no destino | `D-REJ-CROSS` |

Cada cenário confere status, regra decisora e score; os casos negativos
também verificam que a evidência ausente, clara ou expirada não é convertida
automaticamente em rejeição.

## Cenários geográficos do G01

`test_geolocalizacao.py` documenta a regra geográfica opcional da RFC:

| Cenário | Sinais relevantes | Resultado |
|---|---|---|
| Histórico insuficiente | menos de 10 observações ou menos de 7 dias | `G01 unavailable`, aprovação |
| São Paulo → Porto Alegre | `G01 +6` | `D-OK`; localização isolada não cria fricção |
| Dois centros habituais | São Paulo e Porto Alegre, ambos qualificados | `G01 not_triggered` |
| Distância abaixo de 300 km | São Paulo → Campinas | `G01 not_triggered` |
| Observações nas últimas 24h | histórico recente excluído da janela | `G01 unavailable` |
| Distante + valor atípico + rajada | `G01 + P06 + P04 + P07` | `D-PAY`, `two_factor_required` |
| ATO completo com distância | `G01 + P03 + P04 + P06 + P07 + L02` | `D-REJ-ATO`, `rejected` |

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
