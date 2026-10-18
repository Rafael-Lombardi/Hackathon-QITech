# Guia para contribuidores

Este documento é para quem vai ler, alterar ou testar o código. O [README principal](README.md) explica o produto, a política e os cenários para uma pessoa avaliadora; o [`bootcamp-base-api/README.md`](bootcamp-base-api/README.md) explica como subir a API e usar suas rotas.

## Mapa rápido

O controller coordena o caso de uso; o pacote `antifraud` calcula uma análise a partir de um retrato pronto.

| Conceito | Onde está | Responsabilidade |
|---|---|---|
| Entrada HTTP | `bootcamp-base-api/src/resources/`, `schemas/` | validar o JSON e encaminhar a rota |
| Orquestração | `src/controllers/pix_transaction_controller.py` | idempotência, DICT, locks, histórico, contexto, persistência e commit |
| Contexto | `src/antifraud/contract.py` + controller/repositories | Pix normalizado, histórico, cadastro e evidências disponíveis |
| Política | `src/antifraud/policy.py` | versão, catálogo, pesos, janelas, tetos e tabela de decisão |
| Camadas | `src/antifraud/layers/` | avaliar condições independentes e produzir sinais |
| Engine | `src/antifraud/engine.py` | executar camadas, agregar famílias, calcular scores e decidir |
| Persistência | `src/repositories/`, `src/models/`, `database/database.sql` | ler histórico e gravar análise, sinais, evidências e receipt |
| Resposta | `src/dtos/pix_transaction_dto.py` | transformar a análise persistida no JSON público |

### Sinal, família, score e decisão

1. **Sinal:** regra individual (`P04`, `R03`, `DICT01`), com estado `triggered`, `not_triggered` ou `unavailable`, pontos e evidência.
2. **Família/grupo de teto:** sinais relacionados acumulam pontos, por exemplo `payer_velocity` ou `recipient_network`. O teto limita a contribuição em `metadata.family_contributions`.
3. **Scorers lógicos:** o payer scorer agrega as famílias do pagador; o recipient scorer agrega as famílias do recebedor. O código usa o mesmo agregador (`_family_contributions()` e `_axis_risk()`), parametrizado pelo eixo.
4. **Decisão:** `DECISION_TABLE` percorre as regras em ordem. Restrições e rejeições vêm antes de 2FA; `D-OK` é o fallback.

As camadas não leem sinais umas das outras. O `engine.py` é o único ponto que junta sinais, famílias, scores e decisão.

Há três camadas no modelo mental e quatro avaliadores no código: a camada 3 de evidência DICT é dividida em `DictSecurityStatisticsLayer` (`DICT01`) e `DictSecurityLayer` (`DICT02`/`DICT03`). Essa divisão só separa duas fontes de evidência; ambas continuam sendo a mesma camada conceitual do pipeline.

## Onde alterar

| Mudança | Arquivos principais |
|---|---|
| peso, limiar, janela ou teto | `src/antifraud/policy.py` e teste de fronteira |
| regra nova | função em `src/antifraud/layers/` + `CATALOG` + teste de integração |
| ordem da decisão | `DECISION_TABLE` e frase correspondente em `engine.py` |
| campo de entrada | JSON Schema e conversão em `pix_request.py` |
| consulta ou contagem histórica | `src/repositories/` |
| campo da resposta | `src/dtos/pix_transaction_dto.py` |
| tabela | `database/database.sql`, model e re criação do banco local |

## Leitura detalhada

O documento [`bootcamp-base-api/docs/como-o-projeto-e-organizado.md`](bootcamp-base-api/docs/como-o-projeto-e-organizado.md) mostra o caminho completo da requisição, as regras de dependência entre pastas, o ciclo da sessão do banco e as armadilhas mais comuns.

Para executar os testes e a API, siga o [`bootcamp-base-api/README.md`](bootcamp-base-api/README.md). Para entender os cenários de aceitação, veja [`bootcamp-base-api/tests/integration/pix/README.md`](bootcamp-base-api/tests/integration/pix/README.md).
