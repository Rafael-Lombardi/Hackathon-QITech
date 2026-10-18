# [Nome do grupo — preencher]

**Integrantes:** [Integrantes — preencher]

## Como o motor de decisão funciona

Uma API, feita a partir do Base Service ([`bootcamp-base-api/`](bootcamp-base-api/)), que recebe um Pix em `POST /pix/transaction` e decide, antes de o dinheiro sair, entre `approved`, `two_factor_required` (pedir ao cliente uma segunda confirmação, o 2FA) e `rejected`; `GET /pix/transaction/{id}` devolve a análise gravada. Em volta do motor, o controller cuida do contrato, da idempotência, da consulta ao DICT (o diretório do Banco Central que liga chave Pix a conta), da trava e do histórico ([passo a passo](bootcamp-base-api/README.md#6-o-caminho-de-uma-requisição)).

O motor ([`src/antifraud/`](bootcamp-base-api/src/antifraud/)) mora fora do controller, onde o Base Service põe a regra, para não se misturar com trava, sessão e commit ([o porquê](bootcamp-base-api/docs/como-o-projeto-e-organizado.md#por-que-o-motor-é-um-pacote-à-parte)). É um pacote puro e determinístico: recebe o `Context` — o Pix normalizado, o histórico já contado e a resposta do DICT — e roda três camadas de regras, todas com o mesmo contrato:

1. **Validações iniciais** — o que o próprio Pix diz, sem histórico: conta nova, valor alto à noite, mesmo titular.
2. **Sinais sobre o histórico** — regras determinísticas, escritas à mão, sobre o que o pagador, o aparelho, o recebedor e o par já fizeram. Sem fraude rotulada não há modelo a treinar, e cada resposta precisa caber numa frase.
3. **O DICT** — em dois arquivos: a estatística de segurança do recebedor e a marcação de fraude dele. Nesta demonstração quem responde é um mock, com o mesmo formato de resposta.

Nenhuma camada lê o sinal de outra; quem junta é o engine, nos tetos, nas notas e na tabela de decisão. Cada um dos 23 sinais sai `triggered`, `not_triggered` ou `unavailable` (falta de histórico não vira ponto nem atestado); os pontos somam por grupo, com teto, em duas notas de 0 a 100 que não são probabilidade, **pagador** e **destino**, cada uma com a **suficiência das evidências**; e na **tabela de decisão** vale a primeira linha que casar. Tudo volta em `metadata` ([exemplo resolvido](#um-exemplo-resolvido)).

**O relógio é o do negócio.** As janelas de histórico contam em `transaction_date`, a data que vem no payload, do sistema do banco: dez Pix de dez dias mandados hoje são dez dias, e não uma rajada. É o que deixa qualquer cenário deste README ser reproduzido por `curl`, com as datas escolhidas ([como](#reproduzindo-um-cenário-por-curl)). A hora de chegada (`received_at`) fica gravada só para auditoria.

**O DICT** é consultado uma vez por Pix, pelo documento do recebedor, antes de qualquer trava, e responde a estatística de segurança e a marcação de fraude (`null`, `suspicious` ou `confirmed`). Sem marcação é informação: o DICT olhou e não tinha nada contra o documento. DICT fora do ar, lento ou respondendo fora do combinado deixa os três sinais da camada 3 `unavailable`, e o Pix é decidido pelas outras duas.

## Regras

Os 23 sinais, na ordem da resposta. Valores em reais; no payload são centavos (R$ 1.000 é `100000`). Como ler:

- abaixo do nome: camada · família · eixo. **Família** é o rótulo do desafio, que sai na resposta (Velocity é ritmo); **grupo de teto**, por onde os pontos somam;
- **pagador** e **recebedor** são o documento, somando as contas: o laranja — quem cede a conta para receber dinheiro de golpe e repassar — com três contas é um recebedor só;
- "anterior" é todo Pix gravado com `transaction_date` até a do Pix atual; "nos últimos X" é a janela que termina nele. "Hoje" é o dia civil de São Paulo. Rajadas contam qualquer status, para quem insiste depois do 2FA seguir visível; aparelho conhecido, IP conhecido, mediana e relação contam só Pix aprovados, porque o resultado do 2FA não volta para a API;
- **`unavailable`** quando falta histórico ou fonte: cada sinal aplica a própria pré-condição, e os três do DICT dependem de ele ter respondido. Indisponibilidade não é segurança;
- os números estão em [`policy.py`](bootcamp-base-api/src/antifraud/policy.py), e a versão da política (`v0.6.0`) vai gravada em cada análise.

**A conta que guia os pesos:** 2FA pela nota começa em 40, e nenhum sinal chega lá sozinho — o mais pesado, o DICT01, tem 32 —, então cada peso é escolhido pela combinação que leva a 40. As siglas D-* são as linhas da [decisão](#como-as-regras-viram-decisão).

| Regra | Lógica | Parâmetros | Por quê |
|---|---|---|---|
| **P01** Conta pagadora recém-aberta<br>camada 1 · Perfil · pagador | Conta de origem aberta há menos de 7 dias, pela `opening_date` do payload; aberta depois do Pix conta 0. | < 7 dias · **+10** · conta pagadora, teto 10 | Laranja usado como origem: recebe o golpe e repassa. Conta nova não prova fraude, e pesa um quarto dos 40. Uma semana pega o começo da conta, quando o laranja é usado, sem marcar quem a abriu no mês passado. |
| **L01** Valor alto no período noturno<br>camada 1 · Limite · pagador | Hora de São Paulo das 20h às 5h59 **e** valor acima de R$ 1.000. | 20h às 6h · > R$ 1.000 · **+8** · limite, teto 15 | O limite noturno do Banco Central (R$ 1.000 das 20h às 6h), criado contra o sequestro relâmpago. Aqui é só um sinal fraco: o cliente pode ter pedido aumento desse limite, e o payload não diz qual é o dele. |
| **R01** Conta destino recém-aberta<br>camada 1 · Contraparte · destino | Conta de destino do Pix (o ISPB, código de 8 dígitos do banco, mais agência e número) aberta há menos de 7 dias. | < 7 dias · **+12** · rede do destino, teto 40 | O laranja abre a conta para receber o golpe e esvaziar logo. Sozinho passa: conta nova recebendo o primeiro Pix é comum (cenário 03 da RFC). Pesa mais que o P01 porque o pagador já é cliente nosso, e o recebedor não. |
| **REL03** Mesmo titular<br>camada 1 · Contraparte · destino | Documento do pagador igual ao do recebedor. | **-5** · relação, de -10 a +8 | Mandar dinheiro para si mesmo, entre bancos, é comum. Desconta 5, menos que a relação recorrente: autotransferência não é golpe, mas também não basta para aprovar. Tira o L03 e o D-SCAM do caminho: para si mesmo não há teto nem destino inédito. |
| **L02** Valor acima do limite para dispositivo novo<br>camada 2 · Limite · pagador | Aparelho sem Pix aprovado do pagador antes de hoje **e** (valor acima de R$ 200 **ou** soma de hoje nele, com o atual, acima de R$ 1.000). Pré-condição: 3 Pix aprovados do pagador; antes disso, `unavailable`. | > R$ 200 por Pix · > R$ 1.000 no dia · **+10** · limite, teto 15 | A regra do Banco Central para aparelho não cadastrado: o golpista operando a conta da vítima a partir do aparelho dele. Não sabemos o que o banco cadastrou; temos o nosso histórico. "Antes de hoje", e não "nunca": senão a soma do dia seria sempre o próprio Pix, e o 2º e o 3º Pix do dia não entrariam na conta. |
| **L03** Valor acima do teto por Pix<br>camada 2 · Limite · pagador | Pix para terceiro com valor a partir de R$ 50.000; ou a partir de R$ 5.000 quando o pagador não tem nenhum Pix aprovado na base. Nunca `unavailable`; para a própria conta não há teto. | ≥ R$ 50.000 · ≥ R$ 5.000 sem Pix aprovado · **+15** · teto por Pix, teto 15 | "Passou do teto?" é a primeira pergunta do desafio, e todo banco aplica um teto por Pix. Quem nunca teve Pix aprovado aqui tem o teto menor, como a conta recém-aberta tem limite inicial baixo até mostrar a que veio. É o primeiro valor que não sai sem confirmação (D-LIMIT), nunca rejeição. |
| **G01** Localização distante do padrão<br>camada 2 · Localização · pagador | Geolocalização do Pix a mais de 300 km de todo centro habitual do pagador. Centro habitual: Pix aprovados com localização dos últimos 90 dias, até 24 h antes deste, agrupados num raio de 100 km, cada um com 2 dias distintos ou mais. Sem 10 observações em 7 dias não há padrão: `unavailable`. | > 300 km · 10 obs. em 7 dias · **+6** · comportamento, teto 45 | O celular da vítima não viaja com o golpista. Mas gente viaja e GPS erra: pesa só 6, e sozinho passa. Dois centros habituais (casa numa cidade, trabalho noutra) não penalizam a viagem de sempre, e o corte de 24 h impede que a rajada de agora ensine o lugar de agora. |
| **P03** Dispositivo sem Pix aprovado do pagador<br>camada 2 · Perfil · pagador | Nenhum Pix aprovado do pagador, por qualquer conta, com este `device_id`. Pré-condição: 3 Pix aprovados; antes disso, `unavailable`. | **+18** · comportamento, teto 45 | Tomada de conta: a conta operada de outro aparelho. Troca de celular acontece, e 18 sozinho passa; com valor atípico (D-ATO-1) ou IP novo (D-ATO-2), pede 2FA. Só Pix aprovado ensina o aparelho: senão a 2ª tentativa do invasor passaria limpa. Antes de 3 aprovados quase tudo é inédito, e o cliente novo barrado três vezes ficaria preso em 2FA. |
| **P04** Muitas tentativas em 5 min<br>camada 2 · Velocity · pagador | 5 ou mais Pix do pagador nos últimos 5 min, contando o atual. | ≥ 5 em 5 min · **+20** · rajada do pagador, teto 25 | O ritmo de quem esvazia a conta: acesso remoto, malware que dispara transferências, a sequência que a falsa central pede. Quem paga contas faz um Pix de cada vez, e rapidez sozinha passa (20, cenário 08 da RFC). |
| **P05** Montante alto na última hora<br>camada 2 · Montante · pagador | Soma dos Pix do pagador na última hora, com o atual, acima de R$ 8.000. | > R$ 8.000 em 1 h · **+15** · rajada do pagador, teto 25 | O mesmo esvaziamento, visto pelo dinheiro: quem fraciona para passar por baixo de um limite por Pix soma alto na hora. Mais baixo, pegaria quem paga as contas do mês de uma vez; mais alto, deixaria passar o esvaziamento em parcelas. |
| **P06** Valor muito acima do padrão<br>camada 2 · Perfil · pagador | Valor maior que 5 vezes a mediana dos Pix aprovados do pagador **e** a partir de R$ 1.000. Pré-condição: 10 Pix aprovados em 7 dias distintos ou mais; antes disso, `unavailable`. | > 5 × a mediana · ≥ R$ 1.000 · **+18** · comportamento, teto 45 | Na falsa central e no falso parente, a vítima paga muito mais do que costuma. Mediana, e não média: um Pix de R$ 5.000 entre vinte de R$ 50 leva a média a R$ 286. 5 vezes deixa passar a compra maior que o normal; o piso tira o troco. |
| **P07** Vários destinos inéditos em 10 min<br>camada 2 · Velocity · pagador | 3 ou mais recebedores sem Pix aprovado do pagador antes da janela, entre os Pix dele dos últimos 10 min mais o atual. | ≥ 3 em 10 min · **+18** · comportamento, teto 45 | Esvaziar a conta para vários recebedores inéditos de uma vez, como no acesso remoto. Pagar duas pessoas novas em seguida acontece (o racha da conta do bar); a terceira em 10 min já é o desenho do dreno. |
| **P08** Dispositivo usado por vários pagadores<br>camada 2 · Perfil · pagador | 3 ou mais documentos pagadores com este `device_id` nas últimas 24 h, contando o atual. | ≥ 3 em 24 h · **+12** · comportamento, teto 45 | Um aparelho operando contas de várias pessoas é indício de farm de contas, os laranjas da mesma mão. Um casal divide o celular; três documentos num dia já é outra coisa. Família também compartilha, e por isso pesa só 12. |
| **P09** IP sem Pix aprovado do pagador<br>camada 2 · Perfil · pagador | Nenhum Pix aprovado do pagador com este IP (o mesmo IPv6 escrito de dois jeitos é um IP só). Pré-condição: 3 Pix aprovados. | **+6** · comportamento, teto 45 | Rede nova acompanha a tomada de conta. Mas IP de celular e VPN mudam o tempo todo: pesa só 6, e sozinho passa (cenário 16 da RFC). Vale junto do aparelho novo (D-ATO-2). |
| **P10** Dispositivo e IP sem Pix aprovado do pagador<br>camada 2 · Perfil · pagador | As condições do P03 e do P09 ao mesmo tempo. | **+12** · comportamento, teto 45 | Aparelho e rede trocados juntos é mais forte que cada troca. Nunca muda o status — quando dispara, o D-ATO-2 ou uma linha anterior já decide —, mas pesa na nota gravada, que é a que se usa para calibrar. |
| **R02** Muitas entradas no mesmo destino<br>camada 2 · Velocity · destino | 5 ou mais Pix para o mesmo recebedor, por qualquer conta dele, nos últimos 10 min, contando o atual. | ≥ 5 em 10 min · **+16** · rede do destino, teto 40 | Isca de ganho fácil (robô multiplicador, loja falsa) e laranja recebem em rajada. A janela dobra a do P04: as entradas vêm de muitas pessoas, cada uma no seu tempo. É a peça da rejeição do destino: sem ela, o destino marcado no DICT no máximo pede 2FA. |
| **R03** Muitos pagadores para o mesmo destino<br>camada 2 · Contraparte · destino | 4 ou mais documentos pagadores distintos para o mesmo recebedor na última hora, contando o atual. | ≥ 4 em 1 h · **+20** · rede do destino, teto 40 | Muita gente pagando o mesmo recebedor em pouco tempo é a cara da isca e do laranja. Pessoa física quase nunca recebe de quatro pessoas na mesma hora; uma loja recebe, e por isso sozinho o R03 passa (cenário 11 da RFC). |
| **R04** Conta nova concentrando pagadores<br>camada 2 · Contraparte · destino | Conta de destino com menos de 7 dias **e** a condição do R03. | **+15** · rede do destino, teto 40 | O laranja recém-aberto que já concentra pagadores. Junta duas evidências que já existem (R01 e R03 somam 32) e é o que leva a rede ao teto de 40: a conta nova com muitos pagadores pede 2FA sozinha (D-RCP), até de um pagador que nunca vimos. |
| **REL01** Destino inédito para o pagador<br>camada 2 · Contraparte · destino | Nenhum Pix aprovado do pagador para este recebedor. | **+8** · relação, de -10 a +8 | Na falsa central, no falso parente e no QR trocado, o dinheiro vai para alguém que o cliente nunca pagou. Mas todo mundo paga alguém pela primeira vez: 8 só pesa somado ao que se sabe do recebedor — e, com valor atípico, é o D-SCAM. |
| **REL02** Relação recorrente<br>camada 2 · Contraparte · destino | 3 ou mais Pix aprovados do pagador para este recebedor, o primeiro há 7 dias ou mais, **e** nenhuma rejeição nem 2FA entre os dois nos últimos 7 dias. | ≥ 3 aprovados · primeiro há ≥ 7 dias · **-8** · relação, de -10 a +8 | O aluguel, a diarista, a mesada: três Pix aprovados, o primeiro há uma semana, já são hábito, e não deveriam pagar fricção. Um 2FA ou uma rejeição recente no par suspende o desconto, para não facilitar justo a tentativa seguinte. |
| **DICT01** Estatística de segurança DICT desfavorável<br>camada 3 · Contraparte · destino | Estatística de segurança do recebedor abaixo de 30. `unavailable` quando o DICT não respondeu no prazo, respondeu erro ou corpo fora do combinado. | < 30 · prazo de 2 s · **+32** · estatística DICT, teto 32 | A estatística diz o que o sistema Pix inteiro registrou do recebedor, coisa que o nosso histórico não vê. Sozinho fica em 32, abaixo dos 40, de propósito: o DICT real atualiza com até 12 h de atraso. Com qualquer outro indício do destino pede 2FA. O mock reduz a estatística a um número; o real devolve contadores por janela. |
| **DICT02** Marcação de fraude suspeita no DICT<br>camada 3 · Contraparte · destino | O DICT devolve `fraud_marker: "suspicious"` para o documento do recebedor. | `suspicious` · **+20** · marcação DICT, teto 25 | A marcação é o que um participante registrou contra o documento (Manual do DICT, caps. 10 e 18), ainda sem confirmação. Sozinha não pede 2FA; pesa junto da rede e é a porteira das rejeições do destino (D-REJ-RCP, D-REJ-CROSS). |
| **DICT03** Marcação de fraude confirmada no DICT<br>camada 3 · Contraparte · destino | O DICT devolve `fraud_marker: "confirmed"` para o documento do recebedor. | `confirmed` · **+25** · marcação DICT, teto 25 | A mais forte das evidências de fora, e mesmo assim não rejeita sozinha: rejeição pede a rajada no destino ou os dois eixos altos. Nunca dispara junto do DICT02 — a marcação é uma só —, e por isso o teto do grupo é o peso dela. |

Os grupos de teto. Somando os tetos, o pagador daria 110 e o destino 105; as duas notas param em 100. O preço do teto é que o segundo indício de um grupo vale menos que o primeiro.

| Grupo (no código) | Sinais | Faixa | Por quê |
|---|---|---|---|
| limite (`limit`) | L01, L02 | 0 a 15 | os dois olham o valor do mesmo Pix: noite e aparelho novo juntos somam 18 e param em 15 |
| teto por Pix (`amount_cap`) | L03 | 0 a 15 | um sinal só, e o teto é o próprio peso; fora do `limit` porque o teto por Pix já decide sozinho (D-LIMIT), e não divide teto com o sinal fraco da noite |
| conta pagadora (`payer_account`) | P01 | 0 a 10 | um sinal só, e o teto é o próprio peso |
| rajada do pagador (`payer_velocity`) | P04, P05 | 0 a 25 | a mesma rajada vista por quantidade e por dinheiro: 35 viram 25 |
| comportamento (`payer_behavior`) | G01, P03, P06, P07, P08, P09, P10 | 0 a 45 | o maior grupo, porque é onde mora a tomada de conta |
| rede do destino (`recipient_network`) | R01, R02, R03, R04 | 0 a 40 | em 40, a conta nova concentrando pagadores (R01 + R03 + R04 = 47) pede 2FA sozinha, pelo D-RCP; a loja madura em rajada (R02 + R03 = 36) continua passando |
| estatística DICT (`dict_security_statistics`) | DICT01 | 0 a 32 | um sinal só, e o teto é o próprio peso: a estatística soma por fora da rede |
| marcação DICT (`dict_security`) | DICT02, DICT03 | 0 a 25 | os dois nunca disparam juntos (a marcação é suspeita OU confirmada), e o teto é o peso da confirmada |
| relação (`relation`) | REL01, REL02, REL03 | -10 a +8 | o único que desce, e no máximo 10: relação antiga não apaga sinal grave |

**O que o código fecha além do texto da RFC:**

- **DICT01 tem teto próprio** (não divide o da rede): dividindo, conta recém-aberta com nota ruim parava no teto e saía aprovada.
- **Conhecido e relação exigem Pix aprovado** (P03, P07, P09, P10, REL01, REL02), e a pré-condição de aparelho e IP conta 3 aprovados: o 2FA não volta para a API e não pode ensinar nem o aparelho nem o IP.
- **L02:** aparelho novo é o sem Pix aprovado antes de hoje; com o "nunca visto" da RFC, a soma do dia seria só o próprio Pix.
- **REL02:** 3 aprovados, o primeiro há 7 dias ou mais, sem rejeição nem 2FA recente: só relação provada.
- **A suficiência só informa:** cada regra tem a própria pré-condição de histórico.
- **D-RCP a partir de 40, sem teto de 69:** rejeição só sai das três linhas D-REJ; o resto do destino alto vai para 2FA.
- **A concorrência** usa um advisory lock transacional pelo documento do pagador, tomado antes de contar o histórico: dois Pix do mesmo pagador passam um de cada vez, e o segundo enxerga o primeiro.

## Como as regras viram decisão

A tabela, em ordem ([`DECISION_TABLE`](bootcamp-base-api/src/antifraud/policy.py)):

| Ordem | Linha | Condição | Status | Por quê |
|---|---|---|---|---|
| 1 | D-REJ-ATO | P03, P04, P06 e P07 disparados, pagador ≥ 70 | `rejected` | aparelho novo, rajada, valor atípico e destinos inéditos ao mesmo tempo: o dreno da conta tomada, e a única rejeição que não precisa do DICT |
| 2 | D-REJ-RCP | R02 e R03 disparados, destino ≥ 55, marcação de fraude do recebedor no DICT | `rejected` | destino que o sistema Pix já marcou recebendo rajada de muita gente |
| 3 | D-REJ-CROSS | pagador ≥ 40 e destino ≥ 40, marcação de fraude do recebedor no DICT | `rejected` | os dois eixos no limiar do 2FA e o destino marcado |
| 4 | D-ATO-1 | P03 e P06 | `two_factor_required` | aparelho novo com valor atípico: a cara da tomada de conta |
| 5 | D-ATO-2 | P03 e P09 | `two_factor_required` | aparelho e rede trocados juntos |
| 6 | D-PAY | pagador ≥ 40 | `two_factor_required` | soma de indícios sobre o pagador |
| 7 | D-LIMIT | L03 | `two_factor_required` | passou do teto por Pix, ou do teto menor de quem nunca teve Pix aprovado: confirmação, seja qual for a nota |
| 8 | D-SCAM | P06 e REL01, sem REL03 | `two_factor_required` | a engenharia social: valor atípico para alguém que o cliente nunca pagou, do próprio aparelho. A vítima autoriza, e a pausa é o que temos |
| 9 | D-DRAIN | P04 e P07 | `two_factor_required` | rajada para destinos inéditos: o dreno em parcelas pequenas, antes de a nota chegar aos 40 |
| 10 | D-RCP | destino ≥ 40 | `two_factor_required` | destino suspeito sem a combinação de rejeição |
| 11 | D-OK | nenhuma das anteriores | `approved` | nenhum caso justificou fricção |

**Por que só três rejeições.** Bloquear um cliente bom também é prejuízo, e rejeitar por nota é o falso positivo mais caro. A tabela rejeita em dois casos: a tomada de conta com todos os sinais juntos, ou um destino que o sistema Pix inteiro já marcou como fraude — a marcação do DICT é a porteira das duas rejeições do destino, e não ponto (os pontos dela já estão no DICT02 e no DICT03). Nota alta sem isso é 2FA.

**Por que 2FA, e para quê.** O 2FA ajuda quando outra pessoa pode estar usando a conta (D-ATO-1, D-ATO-2, D-DRAIN) e quando o valor foge do que o cliente faz (D-PAY, D-LIMIT). No golpe de engenharia social a vítima mesma manda, e confirma: o D-SCAM pausa justo aí, no valor atípico para um destino inédito, porque a pausa é a chance de ela desistir — é o que temos sem o resultado do 2FA voltar para a API.

**Por que 40.** No destino, a rede para em 40: a conta nova concentrando pagadores chega lá sozinha, e a loja madura em rajada (36) não. O DICT01 fica em 32 e chega a 40 com qualquer outro indício do destino (R01 dá 44; REL01, 40; R02 ou R03 pegam a conta já aquecida); as marcações (20 e 25) também precisam de companhia. No pagador, nenhum sinal passa de 20, e as combinações (D-ATO, D-DRAIN, D-SCAM) decidem antes da nota.

**A primeira observação** (cold start) deixa `unavailable` os sinais de histórico daquele lado; decidem a camada 1, o L03 (R$ 5.000 é o teto de quem nunca teve Pix aprovado), a camada 3 e o histórico do outro lado. O Pix de um cliente novo para um laranja em rajada continua barrado pela rede do destino.

**A suficiência** sai dos Pix anteriores (*n*) e dos dias distintos (*d*) de cada eixo: *n* = 0 é `unavailable`; *n* ≥ 30 em *d* ≥ 7 é `high`; *n* ≥ 6 em *d* ≥ 2 é `medium`; o resto, `low`. Não soma ponto: mostra quanto histórico havia por trás da nota.

**O motivo** é a frase da linha que decidiu, com números. Frases reais, copiadas da API:

| Linha | `reasons` |
|---|---|
| D-REJ-ATO | "Risco do pagador em 80 (acima do limiar de 70): muitas tentativas em 5 min (P04), dispositivo sem Pix aprovado do pagador (P03) e valor muito acima do padrão (P06)" |
| D-REJ-RCP | "Risco do destino em 96 (acima do limiar de 55): estatística de segurança DICT desfavorável (DICT01), muitos pagadores para o mesmo destino (R03) e marcação de fraude suspeita no DICT (DICT02)" |
| D-REJ-CROSS | "Risco cruzado (pagador 43, destino 65) com marcação de fraude no DICT" |
| D-ATO-1 | "Dispositivo sem Pix aprovado deste pagador, com valor 6,2x a mediana" |
| D-ATO-2 | "Dispositivo e IP sem Pix aprovado deste pagador, que tem 3 Pix anteriores" |
| D-PAY | "Risco do pagador em 43 (acima do limiar de 40): muitas tentativas em 5 min (P04), vários destinos inéditos em 10 min (P07) e montante alto na última hora (P05)" |
| D-LIMIT | "Valor de R$ 5.000 alcança o teto por Pix de R$ 5.000 de quem nunca teve Pix aprovado" |
| D-SCAM | "Valor 6,2x a mediana para um destino que o pagador nunca pagou" |
| D-DRAIN | "5 tentativas em 5 min e 5 destinos inéditos em 10 min" |
| D-RCP | "Risco do destino em 48 (acima do limiar de 40): muitos pagadores para o mesmo destino (R03), muitas entradas no mesmo destino (R02) e conta nova concentrando pagadores (R04)" |
| D-OK | "Nenhum indício pesou o bastante para pedir 2FA (risco do pagador 8, do destino 0)" |

### Um exemplo resolvido

O pagador tinha 10 Pix aprovados de R$ 1.000, um por dia, sempre do mesmo aparelho e do mesmo IP. Agora manda R$ 6.200, do mesmo IP mas de um aparelho nunca usado, para um recebedor a quem nunca tinha mandado Pix (o cenário 06 da RFC). Resposta real, com os sinais encurtados (`...`):

```json
{
  "key": "b10c302e-eb2a-43a3-bbbd-aa5daeb9b79f",
  "status": "two_factor_required",
  "metadata": {
    "policy_version": "v0.6.0",
    "decision_rule": "D-ATO-1",
    "reasons": ["Dispositivo sem Pix aprovado deste pagador, com valor 6,2x a mediana"],
    "scores": {"payer": 46, "recipient": 8},
    "evidence_sufficiency": {"payer": "medium", "recipient": "unavailable"},
    "signals": [
      ...
      {"code": "L02", "layer": 2, "granularity": "payer", "family": "Limite", "axis": "payer", "status": "triggered", "points": 10, "evidence": {"amount": 620000, "amount_threshold": 20000, "device_day_amount": 620000, "device_seen_before_today": false, "device_day_amount_threshold": 100000}},
      {"code": "L03", "layer": 2, "granularity": "payer", "family": "Limite", "axis": "payer", "status": "not_triggered", "points": 0},
      ...
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

P03 e P06 somam 36 no comportamento, e o L02 soma 10 no limite: pagador 46. No destino, só o REL01: 8. O IP é o de sempre, e o P09 fica calado; o P08 sai `unavailable`, porque o aparelho nunca tinha aparecido; o L03 fica calado, porque R$ 6.200 está longe do teto de R$ 50.000 de quem já tem Pix aprovado. O D-ATO-1 casa antes do D-PAY: a decisão é da combinação, e a nota só confirma. A suficiência do pagador é `medium`: 10 Pix em 10 dias, longe dos 30 que fazem `high`. Os dez `curl` que montam este cenário estão [logo abaixo](#reproduzindo-um-cenário-por-curl).

## Como rodar e testar

Com Docker, Git e o uv ([instalação, e o caminho sem ele](bootcamp-base-api/README.md#0-o-que-você-precisa-ter-instalado)):

```bash
git clone https://github.com/Rafael-Lombardi/Hackathon-QITech.git
cd Hackathon-QITech/bootcamp-base-api
docker compose up -d --build --wait
until curl -sf http://localhost:1080/security-statistics/00000000000 > /dev/null; do sleep 1; done
```

A última linha espera o mock do DICT, que leva até 18 s para atender: um Pix que chegue antes sai com a camada 3 `unavailable`, e o reenvio devolve essa análise para sempre. O Pix de exemplo do desafio:

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

Num banco novo, volta **201** (encurtado; [a resposta inteira, com os 23 sinais](bootcamp-base-api/README.md#post-pixtransaction)):

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
    ...
  }
}
```

Os 8 são do L01: 22h15 com R$ 1.500 passa do limite noturno. A conta de destino, aberta em 10/10 às 9h, tem 7 dias e 13 horas no momento do Pix, e o R01 fica calado; é o primeiro Pix do pagador na base, e os 15 sinais de histórico saem `unavailable`; o DICT responde o padrão (estatística 70, sem marcação), e a camada 3 fica calada.

O GET devolve a mesma análise; o POST repetido dá **200**, e o mesmo `id` com outro conteúdo, **409**:

```bash
curl -s http://localhost:3000/pix/transaction/pix-000123 -H "INTERNAL-TOKEN: default_token"
```

Os testes, com a stack de pé: 198 testes, em uns 20 s.

```bash
uv venv --python 3.11 .venv && uv pip install -r requirements-dev.txt
source .venv/bin/activate
pytest
```

Todos são de integração, só HTTP: nenhum importa `src/`, nenhum grava nada por SQL, e o histórico de cada cenário é montado por POSTs com as datas escolhidas. A suíte cobre os cenários da RFC; os dois lados de cada limiar, os tetos de grupo e as fronteiras 39/40 do D-PAY e do D-RCP; o que entra no histórico (só aprovado ensina aparelho, IP, mediana e relação; rajada conta toda tentativa); a camada 3 inteira, com o DICT lento, com erro, torto e marcando fraude; as três rejeições e os negativos delas; o contrato (27 payloads fora e 8 dentro); o mesmo `id` em corrida e Pix cruzados; e o G01. Mapa: [`tests/integration/pix/README.md`](bootcamp-base-api/tests/integration/pix/README.md); portas ocupadas e banco a recriar: [README do serviço](bootcamp-base-api/README.md#7-quando-dá-errado).

### Reproduzindo um cenário por curl

Como as janelas contam em `transaction_date`, montar dias de histórico é mandar os Pix com as datas desejadas, em ordem. O exemplo resolvido acima, em onze `curl`: dez Pix de R$ 1.000 às 10h, de 04 a 13/10, e o de R$ 6.200 em 14/10, de outro aparelho, para outro destino.

```bash
API=http://localhost:3000/pix/transaction
HDR=(-H "INTERNAL-TOKEN: default_token" -H "Content-Type: application/json")
PAGADOR='"source_account": {"branch": "0001", "account_number": "77001", "opening_date": "2020-01-15T10:00:00-03:00", "owner": {"document_number": "527.196.834-02"}}'
DE_SEMPRE='"destination_account": {"participant": "60701190", "branch": "0001", "account_number": "55001", "opening_date": "2020-01-15T10:00:00-03:00", "owner": {"document_number": "396.814.275-11"}}'
INEDITO='"destination_account": {"participant": "60701190", "branch": "0001", "account_number": "55002", "opening_date": "2020-01-15T10:00:00-03:00", "owner": {"document_number": "804.517.263-34"}}'
for dia in 04 05 06 07 08 09 10 11 12 13; do
  curl -s -o /dev/null -X POST $API "${HDR[@]}" -d "{\"id\": \"pix-cen06-$dia\", \"amount\": 100000, \"transaction_date\": \"2026-10-${dia}T10:00:00-03:00\", $PAGADOR, $DE_SEMPRE, \"source\": {\"device_id\": \"dvc-de-sempre\", \"channel\": \"app\", \"ip\": \"198.51.100.23\"}}"
done
curl -s -X POST $API "${HDR[@]}" -d "{\"id\": \"pix-cen06-final\", \"amount\": 620000, \"transaction_date\": \"2026-10-14T14:00:00-03:00\", $PAGADOR, $INEDITO, \"source\": {\"device_id\": \"dvc-novo\", \"channel\": \"app\", \"ip\": \"198.51.100.23\"}}"
```

O último responde `two_factor_required` pelo `D-ATO-1`, pagador 46 e destino 8, com L02, P03, P06 e REL01 disparados — a mesma conta do exemplo. Mande mais um, com outro `id` e o `device_id` `dvc-de-sempre`, e sai `D-SCAM` (pagador 18, destino 8): o valor atípico do próprio aparelho para um destino inédito. Para ver a camada 3 basta um Pix, de qualquer pagador, para um dos recebedores de demonstração do mock ([`mockserver/README.md`](bootcamp-base-api/mockserver/README.md)):

| Documento do recebedor | O mock responde | Um primeiro Pix de R$ 50 sai |
|---|---|---|
| `999.999.999-99` | estatística 12 e marcação `confirmed` | `two_factor_required` pelo `D-RCP`: DICT01 +32 e DICT03 +25, destino 57 |
| `777.777.777-77` | estatística 70 e marcação `suspicious` | `approved`: só o DICT02 (+20), destino 20 |
| `888.888.888-88` | estatística 70, depois de 3 s | `approved`, com a camada 3 `unavailable`: a API desiste em 2 s |

As receitas de todos os outros cenários estão nos docstrings dos testes, Pix a Pix.

## Modelagem de dados

As tabelas são seis, em [`database.sql`](bootcamp-base-api/database/database.sql), cada uma com o porquê comentado:

| Tabela | O que guarda |
|---|---|
| `pix_transaction_status` | os três status do contrato |
| `pix_signal_status` | os três estados de um sinal |
| `customer` | quem paga ou recebe, um por documento (só dígitos, `UNIQUE`); é pelo documento do pagador que a trava serializa |
| `account` | uma conta de um customer, em qualquer banco: `UNIQUE NULLS NOT DISTINCT (participant, branch, account_number, customer_id)`. Conta conjunta vira uma linha por titular, e o ISPB fica `NULL` na origem, que o contrato não traz |
| `pix_transaction` | um Pix: o payload inteiro (JSONB), as colunas que o motor leu, a decisão explicada e o `received_at` de auditoria. As notas são colunas, para calibrar os pesos com uma consulta; `UNIQUE(external_id)` é a idempotência |
| `pix_transaction_rule_result` | um sinal avaliado por linha, 23 por Pix, com estado, pontos e evidência: é o `metadata.signals` |

**Por que customer e account separados.** Um documento tem contas em bancos diferentes, e o golpe usa isso. As regras do pagador e as do recebedor (R02, R03, REL01) somam todas as contas do `customer`; R01 e R04 olham a idade da conta indicada no Pix. São as duas identidades do destino da RFC: o titular e a conta.

**Os índices** respondem às perguntas das regras, e todos terminam em `transaction_date`, o relógio das janelas: a conta de origem, com `INCLUDE (status_id, amount, device_id, ip, destination_account_id, latitude, longitude)`, para a consulta do pagador sair do índice sem voltar à tabela; a conta de destino, com `INCLUDE (source_account_id)`, pelo mesmo motivo; o par origem–destino (REL01, REL02); o aparelho (P08); e `account(customer_id)`, que leva das contas ao documento.

## Limites conhecidos

- **O resultado do 2FA não volta para a API:** o cliente que troca de celular e de rede juntos cai no D-ATO-2 em todo Pix, até mandar um aprovado de uma rede conhecida; quem teve o 1º Pix barrado em 2FA pelo teto de R$ 5.000 continua no D-LIMIT acima desse valor até ter um Pix menor aprovado.
- **Rejeição no eixo do destino só com marcação do DICT:** a rede que só nós vemos (R01 a R04) vai no máximo a 2FA, por mais feia que seja.
- **A corrida entre pagadores distintos para o mesmo recebedor é aproximada:** a trava é só por pagador, e dois pagadores no mesmo recebedor (ou no mesmo aparelho), no mesmo instante, contam o histórico sem o Pix um do outro — R02, R03 e P08 podem chegar um Pix atrasados. Travar o recebedor poria todos os pagadores de uma loja em fila única.
- **O histórico do pagador não tem janela:** mediana, dias ativos, aparelho e IP conhecidos olham a vida inteira do documento, e o custo cresce com ela. Com o índice cobrindo, a consulta de um pagador com 5 mil Pix leva ~12 ms, sem voltar à tabela; o próximo passo é uma janela de 90 dias, ou um perfil pré-calculado.
- **DICT de mentira:** o real devolve contadores de fraude por documento, em janelas de 90 dias a 60 meses, com até 12 h de atraso (Manual do DICT, cap. 18), e tem cota de 12.000 consultas por minuto por participante (cap. 14); o mock devolve uma nota e uma marcação prontas, e nunca responde 429.
- **Folha de pagamento PJ** com muitos destinos inéditos em minutos cai em D-DRAIN ou D-PAY: custo declarado, até haver um cadastro de folhas.
- **Compra grande atípica numa loja inédita** pede 2FA (D-SCAM): custo declarado, e é a pausa que segura o golpe do falso parente.

Limites de engenharia: [README do serviço](bootcamp-base-api/README.md#9-limites-de-engenharia).

## Números

Medidos na máquina de desenvolvimento, com a stack do compose (um processo, `--reload`): um POST novo leva ~21 ms (mediana), o reenvio ~5 ms e o GET ~4 ms; com 8 clientes ao mesmo tempo, ~65 Pix/s; a suíte, 198 testes em ~20 s; cada Pix ocupa ~5 KB no banco (uns 100 GB por mês a 20 milhões de Pix, dois terços na tabela de sinais). Os outros números e os próximos passos: [README do serviço](bootcamp-base-api/README.md#8-números).

## Referências

- **RFC do time:** [`RFC5.pdf`](RFC5.pdf) é o texto da v0.4; a [RFC v0.5, no Notion](https://app.notion.com/p/RFC-v0-5-Modelagem-de-Risco-Antifraude-Pix-1-3fc6b8cbe2cc8022aeeaf6e39530a099), é a referência operacional. A política implementada é a **`v0.6.0`**, e o que ela muda sobre a v0.5 está no cabeçalho de [`policy.py`](bootcamp-base-api/src/antifraud/policy.py): aparelho e IP conhecidos saem do histórico de Pix aprovados (sem cadastro de aparelhos); o relógio das janelas é o do negócio; o teto da rede do destino sobe para 40; entram o L03 e as linhas D-LIMIT, D-SCAM e D-DRAIN; saem as linhas de restrição e de rejeição do pagador por evidência externa, que dependiam de fontes que ninguém consulta. **Desafio:** [`Desafio Antifraude Pix.pdf`](Desafio%20Antifraude%20Pix.pdf).
- **Banco Central:** [Manual Operacional do DICT, v8.5](https://www.bcb.gov.br/content/estabilidadefinanceira/pix/Regulamento_Pix/X_ManualOperacionaldoDICT.pdf) (caps. 10, 13, 14 e 18) e [Dúvidas comuns sobre a API do DICT v2](https://www.bcb.gov.br/content/estabilidadefinanceira/pix/duvidas_comuns_api_v2.html).
- **Históricos sintéticos:** [`Historicos/`](Historicos/) gera Pix rotulados em `status`, campo que o schema recusa; não há script que os mande.
