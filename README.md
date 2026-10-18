# [Nome do grupo — preencher]

**Integrantes:** [Integrantes — preencher]

## Como o motor de decisão funciona

> A modelagem consolidada está na [RFC v0.5 do Notion](https://app.notion.com/p/RFC-v0-5-Modelagem-de-Risco-Antifraude-Pix-1-3fc6b8cbe2cc8022aeeaf6e39530a099), que é a referência operacional.

Uma API, feita a partir do Base Service ([`bootcamp-base-api/`](bootcamp-base-api/)), que recebe um Pix em `POST /pix/transaction` e decide, antes de o dinheiro sair, entre `approved`, `two_factor_required` (pedir ao cliente uma segunda confirmação, o 2FA) e `rejected`; `GET /pix/transaction/{id}` devolve a análise gravada. Em volta do motor, o controller cuida do contrato, da idempotência, da consulta ao DICT (o diretório do Banco Central que liga chave Pix a conta), da trava e do histórico ([passo a passo](bootcamp-base-api/README.md#6-o-caminho-de-uma-requisição)).

O motor ([`src/antifraud/`](bootcamp-base-api/src/antifraud/)) mora fora do controller, onde o Base Service põe a regra, para não se misturar com trava, sessão e commit ([o porquê](bootcamp-base-api/docs/como-o-projeto-e-organizado.md#por-que-o-motor-é-um-pacote-à-parte)). É um pacote puro, determinístico, e roda três camadas em ordem, com o mesmo contrato — recebem o `Context` e devolvem seus sinais:

1. **Validações iniciais** — o que o próprio Pix diz, sem histórico: conta nova, valor alto à noite, mesmo titular.
2. **Sinais e política** — regras determinísticas sobre o histórico, escritas à mão: sem fraude rotulada não há modelo a treinar, e cada resposta precisa caber numa frase.
3. **Evidência de segurança do DICT** — estatísticas e marcações simuladas do recebedor. A política não usa um trust factor único; o endpoint legado do mock mantém esse nome por compatibilidade.

Nenhuma camada lê o sinal de outra; quem junta é o engine, nos tetos, nas notas e na tabela de decisão. Cada sinal sai `triggered`, `not_triggered` ou `unavailable` (falta de histórico não vira ponto nem atestado); os pontos somam por grupo, com teto, em duas notas de 0 a 100 que não são probabilidade, **pagador** e **destino**, cada uma com a **suficiência das evidências**; e na **tabela de decisão** vale a primeira linha que casar. Tudo volta em `metadata` ([exemplo resolvido](#um-exemplo-resolvido)).

### Contrato do DICT nesta demonstração

O DICT é consultado como fonte de evidência de segurança, não como um número universal de confiança. `DICT01` representa estatística desfavorável; `DICT02` e `DICT03`, marcações suspeita e confirmada na fixture. `clear` significa apenas que nenhuma evidência adversa conhecida foi retornada; `unavailable` significa que a fonte não respondeu ou não permitiu concluir. Nenhum dos dois estados prova legitimidade ou fraude. O mock mantém `/trust-factor/...` somente por compatibilidade.

## Regras

Os 22 sinais, na ordem da resposta. Valores em reais; no payload são centavos (R$ 1.000 é `100000`). Como ler:

- abaixo do nome: camada · família · eixo. **Família** é o rótulo do desafio, que sai na resposta (Velocity é ritmo); **grupo de teto**, por onde os pontos somam;
- **pagador** e **recebedor** são o documento, somando as contas: o laranja — quem cede a conta para receber dinheiro de golpe e repassar — com três contas é um recebedor só;
- as janelas contam no tempo do negócio (`transaction_date`), até o Pix atual; "hoje" é o dia civil de São Paulo. Rajadas contam qualquer status, para quem insiste depois do 2FA seguir visível;
- **`unavailable`** quando falta histórico ou fonte: cada sinal aplica sua própria pré-condição; DICT01/02/03 dependem da evidência DICT. Indisponibilidade não significa segurança;
- os números estão em [`policy.py`](bootcamp-base-api/src/antifraud/policy.py), e a versão da política (`v0.5.0`) vai gravada em cada análise. `DICT01` é estatística de segurança mockada; `DICT02`/`DICT03` são evidências antifraude DICT suspeita/confirmada na fixture.

**A conta que guia os pesos:** 2FA pela nota começa em 40, e nenhum sinal chega lá sozinho — o mais pesado, o DICT01, tem 32 —, então cada peso é escolhido pela combinação que leva a 40. As siglas D-* são as linhas da [decisão](#como-as-regras-viram-decisão).

| Regra | Lógica | Parâmetros | Por quê |
|---|---|---|---|
| **P01** Conta pagadora recém-aberta<br>camada 1 · Perfil · pagador | Conta de origem aberta há menos de 7 dias, pela `opening_date` do payload; aberta depois do Pix conta 0. | < 7 dias · **+10** · conta pagadora, teto 10 | Laranja usado como origem: recebe o golpe e repassa. Conta nova não prova fraude, e pesa um quarto dos 40. Uma semana pega o começo da conta, quando o laranja é usado, sem marcar quem a abriu no mês passado. |
| **L01** Valor alto no período noturno<br>camada 1 · Limite · pagador | Hora de São Paulo das 20h às 5h59 **e** valor acima de R$ 1.000. | 20h às 6h · > R$ 1.000 · **+8** · limite, teto 15 | O limite noturno do Banco Central (R$ 1.000 das 20h às 6h), criado contra o sequestro relâmpago. Aqui é só um sinal fraco: o cliente pode ter pedido aumento desse limite, e o payload não diz qual é o dele. |
| **R01** Conta destino recém-aberta<br>camada 1 · Contraparte · destino | Conta de destino do Pix (o ISPB, código de 8 dígitos do banco, mais agência e número) aberta há menos de 7 dias. | < 7 dias · **+12** · rede do destino, teto 35 | O laranja abre a conta para receber o golpe e esvaziar logo. Sozinho passa: conta nova recebendo o primeiro Pix é comum (cenário 03 da RFC). Pesa mais que o P01 porque o pagador já é cliente nosso, e o recebedor não. |
| **REL03** Mesmo titular<br>camada 1 · Contraparte · destino | Documento do pagador igual ao do recebedor. | **-5** · relação, de -10 a +8 | Mandar dinheiro para si mesmo, entre bancos, é comum. Desconta 5, menos que a relação recorrente: autotransferência não é golpe, mas também não basta para aprovar. |
| **L02** Valor acima do limite para dispositivo novo<br>camada 2 · Limite · pagador | Aparelho sem Pix aprovado do pagador antes de hoje **e** (valor acima de R$ 200 **ou** soma de hoje nele, com o atual, acima de R$ 1.000). | > R$ 200 por Pix · > R$ 1.000 no dia · **+10** · limite, teto 15 | A regra do Banco Central para aparelho não cadastrado: o golpista operando a conta da vítima a partir do aparelho dele. Não sabemos o que o banco cadastrou; temos o nosso histórico. |
| **P03** Dispositivo sem Pix aprovado do pagador<br>camada 2 · Perfil · pagador | Nenhum Pix aprovado do pagador, por qualquer conta, com este `device_id`. | **+18** · comportamento, teto 45 | Tomada de conta: a conta operada de outro aparelho. Troca de celular acontece, e 18 sozinho passa; com valor atípico (D-ATO-1) ou IP novo (D-ATO-2), pede 2FA. Só Pix aprovado ensina o aparelho: senão a 2ª tentativa do invasor passaria limpa. |
| **P04** Muitas tentativas em 5 min<br>camada 2 · Velocity · pagador | 5 ou mais Pix do pagador nos últimos 5 min, contando o atual. | ≥ 5 em 5 min · **+20** · rajada do pagador, teto 25 | O ritmo de quem esvazia a conta: acesso remoto, malware que dispara transferências, a sequência que a falsa central pede. Quem paga contas faz um Pix de cada vez, e rapidez sozinha passa (20, cenário 08 da RFC). |
| **P05** Montante alto na última hora<br>camada 2 · Montante · pagador | Soma dos Pix do pagador na última hora, com o atual, acima de R$ 8.000. | > R$ 8.000 em 1 h · **+15** · rajada do pagador, teto 25 | O mesmo esvaziamento, visto pelo dinheiro: quem fraciona para passar por baixo de um limite por Pix soma alto na hora. Mais baixo, pegaria quem paga as contas do mês de uma vez; mais alto, deixaria passar o esvaziamento em parcelas. |
| **P06** Valor muito acima do padrão<br>camada 2 · Perfil · pagador | Valor maior que 5 vezes a mediana dos Pix aprovados do pagador **e** a partir de R$ 1.000. | > 5 × a mediana · ≥ R$ 1.000 · **+18** · comportamento, teto 45 | Na falsa central e no falso parente, a vítima paga muito mais do que costuma. Mediana, e não média: um Pix de R$ 5.000 entre vinte de R$ 50 leva a média a R$ 286. 5 vezes deixa passar a compra maior que o normal; o piso tira o troco. |
| **P07** Vários destinos inéditos em 10 min<br>camada 2 · Velocity · pagador | 3 ou mais recebedores sem Pix aprovado do pagador antes da janela, entre os Pix dele dos últimos 10 min mais o atual. | ≥ 3 em 10 min · **+18** · comportamento, teto 45 | Esvaziar a conta para vários recebedores inéditos de uma vez, como no acesso remoto. Pagar duas pessoas novas em seguida acontece (o racha da conta do bar); a terceira em 10 min já é o desenho do dreno. |
| **P08** Dispositivo usado por vários pagadores<br>camada 2 · Perfil · pagador | 3 ou mais documentos pagadores com este `device_id` nas últimas 24 h, contando o atual. | ≥ 3 em 24 h · **+12** · comportamento, teto 45 | Um aparelho operando contas de várias pessoas é indício de farm de contas, os laranjas da mesma mão. Um casal divide o celular; três documentos num dia já é outra coisa. Família também compartilha, e por isso pesa só 12. |
| **P09** IP sem Pix aprovado do pagador<br>camada 2 · Perfil · pagador | Nenhum Pix aprovado do pagador com este IP (o mesmo IPv6 escrito de dois jeitos é um IP só). | **+6** · comportamento, teto 45 | Rede nova acompanha a tomada de conta. Mas IP de celular e VPN mudam o tempo todo: pesa só 6, e sozinho passa (cenário 16 da RFC). Vale junto do aparelho novo (D-ATO-2). |
| **P10** Dispositivo e IP sem Pix aprovado do pagador<br>camada 2 · Perfil · pagador | As condições do P03 e do P09 ao mesmo tempo. | **+12** · comportamento, teto 45 | Aparelho e rede trocados juntos é mais forte que cada troca. Nunca muda o status — quando dispara, o D-ATO-2 ou uma linha anterior já decide —, mas pesa na nota gravada, que é a que se usa para calibrar. |
| **R02** Muitas entradas no mesmo destino<br>camada 2 · Velocity · destino | 5 ou mais Pix para o mesmo recebedor, por qualquer conta dele, nos últimos 10 min, contando o atual. | ≥ 5 em 10 min · **+16** · rede do destino, teto 35 | Isca de ganho fácil (robô multiplicador, loja falsa) e laranja recebem em rajada. A janela dobra a do P04: as entradas vêm de muitas pessoas, cada uma no seu tempo. É a peça da rejeição: sem ela, a conta nova com muitos pagadores no máximo pede 2FA (D-RCP). |
| **R03** Muitos pagadores para o mesmo destino<br>camada 2 · Contraparte · destino | 4 ou mais documentos pagadores distintos para o mesmo recebedor na última hora, contando o atual. | ≥ 4 em 1 h · **+20** · rede do destino, teto 35 | Muita gente pagando o mesmo recebedor em pouco tempo é a cara da isca e do laranja. Pessoa física quase nunca recebe de quatro pessoas na mesma hora; uma loja recebe, e por isso sozinho o R03 passa (cenário 11 da RFC). |
| **R04** Conta nova concentrando pagadores<br>camada 2 · Contraparte · destino | Conta de destino com menos de 7 dias **e** a condição do R03. | **+15** · rede do destino, teto 35 | O laranja recém-aberto que já concentra pagadores. Junta duas evidências que já existem: R01 e R03 somam 32, e o R04 só leva a rede ao teto. Nunca muda o status; fica pela nota gravada e pelo motivo legível. |
| **REL01** Destino inédito para o pagador<br>camada 2 · Contraparte · destino | Nenhum Pix aprovado do pagador para este recebedor. | **+8** · relação, de -10 a +8 | Na falsa central, no falso parente e no QR trocado, o dinheiro vai para alguém que o cliente nunca pagou. Mas todo mundo paga alguém pela primeira vez: 8 só pesa somado ao que se sabe do recebedor. |
| **REL02** Relação recorrente<br>camada 2 · Contraparte · destino | 3 ou mais Pix aprovados do pagador para este recebedor, o primeiro há 7 dias ou mais, **e** nenhuma rejeição nem 2FA entre os dois nos últimos 7 dias. | ≥ 3 aprovados · primeiro há ≥ 7 dias · **-8** · relação, de -10 a +8 | O aluguel, a diarista, a mesada: três Pix aprovados, o primeiro há uma semana, já são hábito: não deveriam pagar fricção. Um 2FA ou uma rejeição recente no par suspende o desconto, para não facilitar justo a tentativa seguinte. |
| **DICT01** Estatística de segurança desfavorável<br>camada 3 · Contraparte · destino | Estatística de segurança do recebedor abaixo de 30. | < 30 · prazo de 2 s · **+32** · estatística DICT, teto 32 | O DICT fornece evidências institucionais sobre a chave/recebedor. Aqui a resposta é uma simplificação mockada: não é um trust factor universal nem prova de legitimidade. |
| **DICT02** Marcação de fraude suspeita no DICT<br>camada 3 · Contraparte · destino | O DICT possui uma marcação de fraude suspeita para o recebedor. | fixture `suspicious` · **+20** · evidência DICT, teto 25 | Evidência adversa declarada, mas ainda não confirmada; participa das rejeições demonstrativas quando combinada com outros sinais. |
| **DICT03** Marcação de fraude confirmada na fixture<br>camada 3 · Contraparte · destino | A fixture possui confirmação de fraude para o recebedor. | fixture `confirmed_in_mock` · **+25** · evidência DICT, teto 25 | Evidência mais forte da demonstração; não é confirmação regulatória nem prova produzida pelo DICT produtivo. |

Os grupos de teto. O pagador chega no máximo a 95, e o destino a 75; o preço do teto é que o segundo indício de um grupo vale menos que o primeiro.

| Grupo (no código) | Sinais | Faixa | Por quê |
|---|---|---|---|
| limite (`limit`) | L01, L02 | 0 a 15 | os dois olham o valor do mesmo Pix: noite e aparelho novo juntos somam 18 e param em 15 |
| conta pagadora (`payer_account`) | P01 | 0 a 10 | um sinal só, e o teto é o próprio peso |
| rajada do pagador (`payer_velocity`) | P04, P05 | 0 a 25 | a mesma rajada vista por quantidade e por dinheiro: 35 viram 25 |
| comportamento (`payer_behavior`) | P03, P06, P07, P08, P09, P10 | 0 a 45 | o maior grupo, porque é onde mora a tomada de conta |
| rede do destino (`recipient_network`) | R01, R02, R03, R04 | 0 a 35 | abaixo de 40: a rede sozinha nunca pede 2FA, e a combinação completa é a rejeição (D-REJ) |
| estatística DICT (`recipient_trust`, nome legado no código) | DICT01 | 0 a 32 | um sinal só, e o teto é o próprio peso: a evidência DICT soma por fora da rede |
| relação (`relation`) | REL01, REL02, REL03 | -10 a +8 | o único que desce, e no máximo 10: relação antiga não apaga sinal grave |

**Ajustes à RFC v0.3:**

- **DICT01 (camada 3) é novo**, com teto próprio desde a versão histórica v0.3.2: dividindo o da rede, conta recém-aberta com nota ruim parava em 35, aprovada.
- **Conhecido e relação exigem Pix aprovado** (P03, P07, P09, P10, REL01, REL02), e a pré-condição de aparelho e IP conta 3 aprovados: o 2FA não volta para a API e não pode ensinar o aparelho.
- **L02:** aparelho novo é o sem Pix aprovado antes de hoje; com o "nunca visto" da RFC, a soma do dia seria só o próprio Pix.
- **REL02:** 3 aprovados, o primeiro há 7 dias ou mais, sem rejeição nem 2FA recente: só relação provada.
- **A suficiência só informa:** cada regra tem a própria pré-condição de histórico.
- **D-RCP a partir de 40, sem o teto de 69:** com o destino em até 75, os piores iriam para o D-OK; vão para 2FA, porque rejeição só sai do D-REJ.
- **A concorrência** usa advisory lock transacional pelo documento do pagador e lock determinístico das linhas de customer.

## Como as regras viram decisão

A tabela, em ordem ([`DECISION_TABLE`](bootcamp-base-api/src/antifraud/policy.py)):

| Ordem | Linha | Condição | Status | Por quê |
|---|---|---|---|---|
| 1 | D-RESTRICT-SIM | Restrição simulada ativa | `rejected` | restrição explícita prevalece sobre score |
| 2 | D-REJ-ATO | combinação extrema de tomada de conta | `rejected` | risco operacional extremo no pagador |
| 3 | D-REJ-PAY | comportamento extremo do pagador + evidência DICT adversa | `rejected` | rejeição demonstrativa com evidência adicional |
| 4 | D-REJ-RCP | rede suspeita do recebedor + evidência DICT adversa | `rejected` | destino com padrão de recebimento incompatível |
| 5 | D-REJ-CROSS | risco alto nos dois eixos + evidência adversa no recebedor | `rejected` | risco combinado de pagador e recebedor |
| 6 | D-ATO-1 | dispositivo não familiar + valor atípico | `two_factor_required` | possível tomada de conta |
| 7 | D-ATO-2 | dispositivo e IP não familiares | `two_factor_required` | ambiente de acesso trocado |
| 8 | D-PAY | risco do pagador ≥ 40 | `two_factor_required` | soma de indícios sobre o pagador |
| 9 | D-RCP | risco do recebedor ≥ 40 | `two_factor_required` | destino suspeito sem combinação de rejeição |
| 10 | D-OK | nenhuma das anteriores | `approved` | nenhum caso justificou fricção |

**Por que há rejeições com evidência.** O 2FA ajuda quando outra pessoa pode estar usando a conta, mas não quando a vítima foi convencida a pagar: ela confirma, e o dinheiro sai. Por isso a política reserva rejeições para combinações fortes de risco e evidência adversa; os casos intermediários passam por 2FA. As quatro rejeições demonstrativas (`D-REJ-ATO`, `D-REJ-PAY`, `D-REJ-RCP` e `D-REJ-CROSS`) não são prova de fraude nem regra regulatória.

**Por que 40.** No destino, a rede para em 35 e só passa de 40 com o REL01 ou o DICT01; o DICT01 chega lá com qualquer outro indício do destino (R01 dá 44; REL01, 40; R02 ou R03 pegam a conta já aquecida).

**A primeira observação** (cold start) deixa `unavailable` as regras de histórico daquele lado; decidem a camada 1, a camada 3 e o histórico do outro lado, e o Pix de um cliente novo para um laranja em rajada continua rejeitado.

**A suficiência** sai dos Pix anteriores (*n*) e dos dias distintos (*d*) de cada eixo: *n* = 0 é `unavailable`; *n* ≥ 30 em *d* ≥ 7 é `high`; *n* ≥ 6 em *d* ≥ 2 é `medium`; o resto, `low`. Não soma ponto: mostra quanto histórico havia por trás da nota.

**A camada 3** só adiciona evidência de segurança do DICT ao risco do destino. Consulta `clear` não significa legitimidade; DICT fora do ar, lento ou torto deixa os sinais DICT `unavailable`, e a política não transforma ausência em segurança.

**O motivo** é a frase da linha que decidiu, com números. Frases reais:

| Linha | `reasons` |
|---|---|
| D-REJ | "Conta destino aberta há 2 dias, com 5 pagadores distintos na última hora e 5 tentativas em 10 min" |
| D-ATO-1 | "Dispositivo sem Pix aprovado deste pagador, com valor 6,2x a mediana" |
| D-ATO-2 | "Dispositivo e IP sem Pix aprovado deste pagador, que tem 3 Pix anteriores" |
| D-PAY | "Risco do pagador em 43 (2FA a partir de 40): muitas tentativas em 5 min (P04), vários destinos inéditos em 10 min (P07) e montante alto na última hora (P05)" |
| D-RCP | "Risco do destino em 44 (2FA a partir de 40): estatística de segurança DICT desfavorável (DICT01) e conta destino recém-aberta (R01)" |
| D-OK | "Nenhum indício pesou o bastante para pedir 2FA (risco do pagador 8, do destino 0)" |

### Um exemplo resolvido

O pagador tinha 10 Pix aprovados de R$ 1.000, um por dia, sempre do mesmo aparelho e do mesmo IP. Agora manda R$ 6.200, do mesmo IP mas de um aparelho nunca usado, para um recebedor a quem nunca tinha mandado Pix. Resposta real, com os sinais encurtados (`...`):

```json
{
  "key": "aa80f536-0ec7-42d1-b9e8-6e76fbf10fd8",
  "status": "two_factor_required",
  "metadata": {
    "policy_version": "v0.5.0",
    "decision_rule": "D-ATO-1",
    "reasons": ["Dispositivo sem Pix aprovado deste pagador, com valor 6,2x a mediana"],
    "scores": {"payer": 46, "recipient": 8},
    "evidence_sufficiency": {"payer": "medium", "recipient": "unavailable"},
    "signals": [
      {"code": "L02", "layer": 2, "granularity": "payer", "family": "Limite", "axis": "payer", "status": "triggered", "points": 10, "evidence": {"amount": 620000, "amount_threshold": 20000, "device_day_amount": 620000, "device_seen_before_today": false, "device_day_amount_threshold": 100000}},
      {"code": "P03", "layer": 2, "granularity": "payer", "family": "Perfil", "axis": "payer", "status": "triggered", "points": 18, "evidence": {"payer_prior_tx": 10, "device_seen_before": false}},
      ...
      {"code": "P06", "layer": 2, "granularity": "payer", "family": "Perfil", "axis": "payer", "status": "triggered", "points": 18, "evidence": {"ratio": 6.2, "amount": 620000, "median_amount": 100000, "ratio_threshold": 5, "amount_threshold": 100000}},
      ...
      {"code": "P08", "layer": 2, "granularity": "device", "family": "Perfil", "axis": "payer", "status": "unavailable", "points": 0},
      {"code": "P09", "layer": 2, "granularity": "payer", "family": "Perfil", "axis": "payer", "status": "not_triggered", "points": 0},
      ...
      {"code": "REL01", "layer": 2, "granularity": "pair", "family": "Contraparte", "axis": "recipient", "status": "triggered", "points": 8, "evidence": {"pair_prior_approved": 0}},
      ...
    ]
  }
}
```

P03 e P06 somam 36 no comportamento, e o L02 soma 10 no limite: pagador 46. No destino, só o REL01: 8. O IP é o de sempre, e o P09 fica calado; o P08 sai `unavailable`, porque o aparelho nunca tinha aparecido. A suficiência do pagador é `medium`: 10 Pix em 10 dias, longe dos 30 que fazem `high`.

## Como rodar e testar

Com Docker, Git e o uv ([instalação, e o caminho sem ele](bootcamp-base-api/README.md#0-o-que-você-precisa-ter-instalado)):

```bash
git clone https://github.com/Rafael-Lombardi/Hackathon-QITech.git
cd Hackathon-QITech/bootcamp-base-api
docker compose up -d --build --wait
until curl -sf http://localhost:1080/trust-factor/00000000000 > /dev/null; do sleep 1; done
```

A última linha espera o mock do DICT, que leva até 18 s para atender: um Pix que chegue antes sai com o DICT01 `unavailable`, e o reenvio devolve essa análise para sempre. O Pix de exemplo do desafio:

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

Num banco novo, volta **201** (encurtado):

```json
{
  "key": "b1f7f4da-ee18-403d-903d-9cd63c41fa60",
  "status": "approved",
  "metadata": {
    "policy_version": "v0.5.0",
    "decision_rule": "D-OK",
    "reasons": ["Nenhum indício pesou o bastante para pedir 2FA (risco do pagador 8, do destino 0)"],
    "scores": {"payer": 8, "recipient": 0},
    ...
  }
}
```

O GET devolve a mesma análise; o POST repetido dá **200**, e o mesmo `id` com outro conteúdo, **409**. Os testes, com a stack de pé, levam uns 12 s:

```bash
uv venv --python 3.11 .venv && uv pip install -r requirements-dev.txt
source .venv/bin/activate
pytest
```

Todos são de integração; nem o motor puro tem unitário, porque o risco está na contagem SQL do histórico e na montagem do `Context`. A suíte cobre os 18 cenários históricos da RFC; os dois lados dos limiares que a RFC lista, mais P07, P08, REL02, DICT01, a pré-condição de 3 aprovados, a suficiência e o teto do corpo; D-PAY e D-RCP em 39 e 40; os tetos de grupo que dá para passar; o mesmo `id` em corrida, Pix cruzados, conta conjunta e o R$ 1 milhão da política histórica v0.3.2. Mapa: [`tests/integration/pix/README.md`](bootcamp-base-api/tests/integration/pix/README.md); portas ocupadas e banco a recriar: [README do serviço](bootcamp-base-api/README.md#7-quando-dá-errado).

## Modelagem de dados

Seis tabelas, em [`database.sql`](bootcamp-base-api/database/database.sql), cada uma com o porquê comentado:

| Tabela | O que guarda |
|---|---|
| `pix_transaction_status` | os três status do contrato |
| `pix_signal_status` | os três estados de um sinal |
| `customer` | quem paga ou recebe, um por documento (só dígitos, `UNIQUE`); é a linha que a trava segura |
| `account` | uma conta de um customer, em qualquer banco: `UNIQUE NULLS NOT DISTINCT (participant, branch, account_number, customer_id)`. Conta conjunta vira uma linha por titular, e o ISPB fica `NULL` na origem, que o contrato não traz |
| `pix_transaction` | um Pix: o payload inteiro (JSONB), as colunas que o motor leu e a decisão explicada. As notas são colunas, para calibrar os pesos com uma consulta; `UNIQUE(external_id)` é a idempotência |
| `pix_transaction_rule_result` | um sinal avaliado por linha, 22 por Pix, com estado, pontos e evidência: é o `metadata.signals` |

**Por que customer e account separados.** Um documento tem contas em bancos diferentes, e o golpe usa isso. As regras do pagador e as do recebedor (R02, R03, REL01) somam todas as contas do `customer`; R01 e R04 olham a idade da conta indicada no Pix. São as duas identidades do destino da RFC v0.3: o titular e a conta. Os índices respondem às perguntas das regras: conta de origem, conta de destino e aparelho, todos terminando em `transaction_date`, mais `account(customer_id)`, que leva das contas ao documento.

## Limites conhecidos

- **O primeiro Pix não tem teto de valor:** só o L01 olha o valor. R$ 1 milhão às 3h, de uma conta aberta ontem, para conta antiga com nota boa no DICT, sai aprovado (pagador 18); para conta nova com nota ruim, pede 2FA (destino 44).
- **O resultado do 2FA não volta para a API:** o cliente antigo que troca de celular e de rede juntos cai no D-ATO-2 em todo Pix, até mandar um de uma rede conhecida.
- **As linhas de combinação quase não mudam o status:** o D-ATO-1 nunca muda (o L02 dispara junto, e o pagador passa de 40); o D-ATO-2, só no Pix pequeno. Valem pelo motivo preciso.
- **Cota do DICT real:** 12.000 consultas de estatísticas por minuto por participante, balde de 36.000 (Manual do DICT, cap. 14); vazio, responde 429 (cap. 13), e num pico o DICT01 sairia `unavailable` em massa, coisa que o mock não reproduz.
- **DICT de mentira:** o real devolve contadores de fraude por documento, em janelas de 90 dias a 60 meses, com até 12 h de atraso (cap. 18); o mock, uma nota pronta.

Limites de engenharia: [README do serviço](bootcamp-base-api/README.md#9-limites-de-engenharia).

## Números

Na máquina de desenvolvimento, um POST novo leva de 16 a 19 ms, e um processo faz de 85 a 100 Pix/s. Os outros números, as notas de escala e os próximos passos: [README do serviço](bootcamp-base-api/README.md#8-números).

## Referências

- **RFC v0.3 do time:** [`RFC3.pdf`](RFC3.pdf). **Desafio:** [`Desafio Antifraude Pix.pdf`](Desafio%20Antifraude%20Pix.pdf).
- **Banco Central:** [Manual Operacional do DICT, v8.5](https://www.bcb.gov.br/content/estabilidadefinanceira/pix/Regulamento_Pix/X_ManualOperacionaldoDICT.pdf) (caps. 13, 14 e 18) e [Dúvidas comuns sobre a API do DICT v2](https://www.bcb.gov.br/content/estabilidadefinanceira/pix/duvidas_comuns_api_v2.html).
- **Históricos sintéticos:** [`Historicos/`](Historicos/) gera Pix rotulados em `status`, campo que o schema recusa; não há script que os mande.
