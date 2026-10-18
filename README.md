# [Nome do grupo]

**Integrantes:** [nomes]

## Como o motor de decisão funciona

O serviço recebe um Pix, valida o contrato, consulta o estado autoritativo e o histórico, avalia evidências do DICT e produz uma decisão determinística antes da autorização: `approved`, `two_factor_required` ou `rejected`.

O controller coordena idempotência por `external_id`, locks, histórico e persistência. O pacote `antifraud` recebe um `Context` pronto e devolve sinais, scores separados para pagador e recebedor e a regra que decidiu. O resultado completo fica gravado e pode ser consultado pelo `GET`.

```mermaid
flowchart TD
    A[POST /pix/transaction] --> B[Validação + busca external_id]
    B --> C[DICT + locks + Context]
    C --> D[Restrições autoritativas]
    C --> E[Camadas de sinais]
    E --> F[Payer scorer]
    E --> G[Recipient scorer]
    D --> H[Política determinística]
    F --> H
    G --> H
    H --> I[Análise + sinais + evidências]
    I --> J[(PostgreSQL)]
    J --> K["GET /pix/transaction/{id}"]
```

As restrições são vetos explícitos e vêm antes dos scores. Os scorers são lógicos: o código compartilha o agregador, mas mantém os eixos separados. O DICT é uma fonte de evidência, não uma probabilidade de fraude. `unavailable` não significa segurança.

As restrições autoritativas são vetos operacionais, avaliados antes dos scores.

## Regras

As regras são sinais independentes. Cada uma produz `triggered`, `not_triggered` ou `unavailable`, com evidência e pontos. Os pontos acumulam em famílias com teto; depois são agregados nos scores do pagador e do recebedor. Pesos e limiares estão em [`policy.py`](bootcamp-base-api/src/antifraud/policy.py).

| Regra | Lógica | Parâmetros | Por quê |
|---|---|---|---|
| P01 | conta pagadora recém-aberta | < 7 dias, +10 | conta nova pode ser laranja, mas não prova fraude |
| L01 | valor alto no período noturno | 20h–6h e > R$ 1.000, +8 | sequestro/acesso fora do padrão; sinal fraco |
| R01 | conta destino recém-aberta | < 7 dias, +12 | risco de conta laranja recém-criada |
| REL03 | mesmo titular | documentos iguais, -5 | autotransferência é comum |
| L02 | valor alto em device não confiável | > R$ 200 ou > R$ 1.000 no dia, +10 | operação em aparelho sem vínculo autoritativo |
| G01 | localização distante do padrão | > 300 km com histórico suficiente, +6 | ajuda a detectar tomada de conta, sem bloquear sozinha |
| P03 | device não confiável | sem vínculo ativo, +18 | aparelho não cadastrado aumenta risco de ATO |
| P04 | muitas tentativas | ≥ 5 em 5 min, +20 | rajada de transferências |
| P05 | montante alto | > R$ 8.000 em 1 h, +15 | fracionamento/saída rápida de dinheiro |
| P06 | valor atípico | > 5× mediana e ≥ R$ 1.000, +18 | valor muito acima do hábito aprovado |
| P07 | destinos inéditos | ≥ 3 em 10 min, +18 | vários recebedores novos sugerem drenagem |
| P08 | device compartilhado | ≥ 3 pagadores em 24 h, +12 | um aparelho operando várias contas |
| P09 | IP sem aprovação anterior | nenhum Pix aprovado no IP, +6 | rede nova é indício fraco |
| P10 | device e IP novos | P03 + P09, +12 | duas mudanças simultâneas são mais fortes |
| R02 | muitas entradas no destino | ≥ 5 Pix em 10 min, +16 | recebedor em rajada |
| R03 | muitos pagadores no destino | ≥ 4 documentos em 1 h, +20 | concentração de pagadores |
| R04 | conta nova concentrando pagadores | R01 + R03, +15 | combinação típica de conta laranja |
| REL01 | destino inédito | nenhum Pix aprovado no par, +8 | primeira relação exige mais contexto |
| REL02 | relação recorrente | ≥ 3 aprovados, primeiro ≥ 7 dias, -8 | hábito comprovado reduz fricção |
| DICT01 | estatística DICT desfavorável | nota < 30, +32 | evidência institucional adversa |
| DICT02 | marcação suspeita | fixture `suspicious`, +20 | evidência externa ainda não confirmada |
| DICT03 | marcação confirmada | fixture `confirmed_in_mock`, +25 | evidência externa mais forte |

Observação e aprovação não são a mesma coisa: qualquer tentativa conta para rajadas e volume; somente `approved` ensina device/IP, mediana, destinos conhecidos e relações. O cadastro autoritativo de devices é independente do histórico.

## Como as regras viram decisão

A política percorre a tabela na ordem abaixo; a primeira condição que casar vence:

| Ordem | Regra | Resultado |
|---:|---|---|
| 1 | `D-RESTRICT-SIM` | rejeita restrição autoritativa ativa |
| 2–5 | `D-REJ-ATO`, `D-REJ-PAY`, `D-REJ-RCP`, `D-REJ-CROSS` | rejeitam combinações fortes com evidência suficiente |
| 6–7 | `D-ATO-1`, `D-ATO-2` | pedem 2FA para possível tomada de conta |
| 8–9 | `D-PAY`, `D-RCP` | pedem 2FA quando um eixo chega a 40 |
| 10 | `D-OK` | aprova como fallback |

`approved` é um rótulo operacional, não uma confirmação posterior de legitimidade. Ainda não há retorno real de 2FA ou ground truth de fraude; por isso a política é determinística e auditável, não um modelo ML calibrado.

## Como rodar e testar

```bash
cd bootcamp-base-api
docker compose up -d --build --wait
until curl -sf http://localhost:1080/security-statistics/00000000000 >/dev/null; do sleep 1; done
```

Envie um Pix para `POST /pix/transaction` com `INTERNAL-TOKEN: default_token`. A resposta nova é `201`; o mesmo payload/idempotency key retorna `200`; o mesmo `id` com outro payload retorna `409`.

### Casos manuais com `curl`

O serviço desativa as páginas automáticas Swagger/OpenAPI; o contrato de entrada está em [`post_pix_transaction.json`](bootcamp-base-api/src/schemas/post_pix_transaction.json). Este caso cria um Pix simples e imprime a análise:

```bash
PIX_ID="manual-$(date +%s)"
NOW="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

curl -i -X POST http://localhost:3000/pix/transaction \
  -H "INTERNAL-TOKEN: default_token" \
  -H "Content-Type: application/json" \
  -d "{\
    \"id\": \"$PIX_ID\",\
    \"amount\": 150000,\
    \"transaction_date\": \"$NOW\",\
    \"source_account\": {\
      \"branch\": \"0001\", \"account_number\": \"88213\",\
      \"opening_date\": \"2020-03-02T10:00:00-03:00\",\
      \"owner\": {\"document_number\": \"12345678909\"}\
    },\
    \"destination_account\": {\
      \"participant\": \"60701190\", \"branch\": \"3675\", \"account_number\": \"10442\",\
      \"opening_date\": \"2020-03-02T10:00:00-03:00\",\
      \"owner\": {\"document_number\": \"98765432100\"}\
    },\
    \"source\": {\
      \"device_id\": \"manual-device\", \"channel\": \"app\",\
      \"ip\": \"198.51.100.23\"\
    }\
  }"
```

Guarde o valor de `PIX_ID` e rode os casos seguintes:

```bash
# 1. Consulta a análise persistida; não reavalia nem chama o DICT novamente.
curl -i "http://localhost:3000/pix/transaction/$PIX_ID" \
  -H "INTERNAL-TOKEN: default_token"

# 2. Reenvio idêntico: retorna 200 e o mesmo corpo da análise.
#    Reexecute o POST anterior mantendo PIX_ID, NOW e o payload iguais.

# 3. Mesmo id com payload diferente: retorna 409.
curl -i -X POST http://localhost:3000/pix/transaction \
  -H "INTERNAL-TOKEN: default_token" \
  -H "Content-Type: application/json" \
  -d "{\"id\":\"$PIX_ID\",\"amount\":1}"

# 4. Corpo inválido: retorna 400 antes de consultar banco ou motor.
curl -i -X POST http://localhost:3000/pix/transaction \
  -H "INTERNAL-TOKEN: default_token" \
  -H "Content-Type: application/json" \
  -d '{"id":"manual-invalid"}'
```

Casos de rejeição e 2FA dependem de histórico, cadastro e evidências seedadas; por isso estão reproduzidos como cenários de integração em `bootcamp-base-api/tests/integration/pix/`, onde cada caso monta seu próprio estado.

```bash
curl -s http://localhost:3000/pix/transaction/<id> \
  -H "INTERNAL-TOKEN: default_token"
```

O `GET` lê a análise persistida: `metadata.decision_rule`, `reasons`, `scores`, `signals`, `family_contributions`, `evidence_sufficiency` e `policy_version` tornam a decisão auditável.

Para a suíte de integração:

```bash
uv venv --python 3.11 .venv
uv pip install -r requirements-dev.txt
source .venv/bin/activate
APP_ENV=test docker compose up -d --build --force-recreate --wait
pytest
```

Os cenários estão organizados por contrato: aceitação (`test_cenarios_aceitacao.py`), decisões/rejeições (`test_decisoes.py`), fronteiras, contrato/idempotência, concorrência, histórico, DICT/geolocalização e indisponibilidade. O relógio é fixável em `APP_ENV=test`, permitindo testar janelas sem depender da hora real.

### Explorar e selecionar um cenário

Para listar exatamente o que o pytest encontrou:

```bash
pytest --collect-only -q tests/integration/pix
```

Para executar um caso específico com nome e progresso detalhados:

```bash
pytest -vv tests/integration/pix/test_decisoes.py::TestDecisoes::test_rejeicao_ato_exige_combinacao_completa
```

Os nomes dos testes são o catálogo dos cenários. Por exemplo:

```bash
pytest -vv tests/integration/pix/test_concorrencia.py
pytest -vv tests/integration/pix/test_historico.py
pytest -vv tests/integration/pix/test_resiliencia.py
```

Cada cenário cria suas próprias identidades e histórico com helpers em `tests/utils/`; não existe uma única database fixture compartilhada com todos os casos. Depois de um teste, é possível inspecionar o estado persistido:

```bash
docker compose exec db psql -U bootcamp -d bootcamp -c \
  "SELECT id, external_id, decision_rule, payer_risk, recipient_risk, received_at FROM pix_transaction ORDER BY id DESC LIMIT 10;"

docker compose exec db psql -U bootcamp -d bootcamp -c \
  "SELECT pix_transaction_id, rule_name, axis, signal_status_id, points FROM pix_transaction_rule_result ORDER BY id DESC LIMIT 30;"

docker compose exec db psql -U bootcamp -d bootcamp -c \
  "SELECT external_id, status_id, received_at, evaluated_at FROM pix_transaction ORDER BY id DESC LIMIT 10;"
```

O banco começa com os catálogos de status; contas, Pix, devices, restrições e evidências são criados pelo cenário que está rodando. Para voltar a um banco vazio, use `docker compose down -v` e suba a stack novamente.

## Modelo de dados

O banco usa um modelo account-scoped: cada `account` combina documento e conta bancária concreta. Não existe `customer` separado: regras que precisam de visão de titular agregam contas pelo documento; regras de conta usam o `account_id` exato.

| Tabela | Papel no domínio |
|---|---|
| `account` | identidade usada como origem/destino; documento, participante, agência, número e data de abertura |
| `device_registry` | cadastro autoritativo de devices confiáveis e revogações; um Pix observado não cadastra device |
| `risk_restriction` | veto temporal explícito para pagador, titular recebedor ou conta recebedora; avaliado antes dos scores |
| `external_risk_assessment` | snapshot de inteligência externa/DICT por eixo (`payer`/`recipient`); não é histórico nem ground truth |
| `pix_transaction` | fato observado, payload original, relógios, scores, regra vencedora e decisão final; `UNIQUE(external_id)` dá idempotência |
| `pix_transaction_rule_result` | uma linha por regra, incluindo `unavailable`, com status, família, eixo, pontos e evidência observada |
| `pix_transaction_risk_evidence` | cópia imutável das restrições e avaliações externas usadas naquela decisão |
| `pix_transaction_status` / `pix_signal_status` | catálogos dos três estados finais e dos três estados de cada regra |

`pix_transaction` aponta para as contas de origem e destino. A decisão e suas duas coleções de explicação são gravadas na mesma transação; não há `receipt`, `lease`, fila ou estado intermediário persistido. Assim, o `GET` só encontra análises completas e cada Pix continua auditável mesmo se as fontes externas mudarem depois.

## Documentação operacional

- [README da API](bootcamp-base-api/README.md): configuração, rotas, mock do DICT e operação local.
- [Testes de integração](bootcamp-base-api/tests/integration/pix/README.md): catálogo dos cenários.

## Referência do desafio

- [`Desafio Antifraude Pix.pdf`](Desafio%20Antifraude%20Pix.pdf)
