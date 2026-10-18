# Como o projeto é organizado

Dentro de `src/` tem onze pastas. Para uma API de quatro endereços, parece muita pasta — e no começo assusta mesmo.

Este texto explica por que elas existem, o que cada uma pode e não pode fazer, e principalmente: **onde você mexe quando quer fazer alguma coisa**.

Não precisa decorar. Leia uma vez para pegar o mapa e volte quando estiver perdido.

---

## 1. O caminho de uma requisição

Toda requisição atravessa as mesmas camadas, sempre na mesma ordem. Este é o `POST /pix/transaction`, que analisa um Pix:

```
  requisição chega
        ↓
  src/middlewares/     cinco cascas, de fora para dentro: identificador
        ↓              único da requisição → log de entrada → INTERNAL-TOKEN
        ↓              (sem ele, para aqui: 403) → tamanho do corpo (sem
        ↓              Content-Length ou acima de 16 KiB, 400) → sessão de
        ↓              banco. A ordem está comentada em src/app.py, onde ela
        ↓              aparece de trás pra frente — o comentário explica por quê
        ↓
  src/resources/       que endereço é esse? quem cuida dele?
        ↓              (o endereço → resource está escrito em src/app.py)
        ↓              (o decorator @SchemaHandler.validate confere o JSON
        ↓               contra src/schemas/ antes da 1ª linha. Torto: 400)
        ↓
  src/controllers/     a ordem do caso de uso. pix_request.py converte o
        ↓              payload; o controller confere a idempotência, pergunta
        ↓              ao DICT, trava o pagador, cria customer e account,
        ↓              conta o histórico, chama o motor, grava e dá o commit
        ↓
        ├──→ src/connectors/     o DICT, lá fora: endereço, prazo e log
        ├──→ src/antifraud/      o motor: recebe o Context, devolve a Analysis
        └──→ src/repositories/   a conversa com o banco — só aqui existe consulta
                    ↓
             src/models/         as tabelas, descritas em Python
                    ↓
                  banco
        ↓
  src/dtos/            traduz a análise gravada no JSON da resposta
        ↓
  resposta sai
```

Repare no que isso compra: quando o código chega no **controller**, o JSON já foi conferido. Quando chega no **motor**, o histórico já foi contado e o DICT já respondeu (ou já falhou). Cada camada confia no trabalho da anterior e só cuida do seu pedaço.

Abra os arquivos e siga o caminho uma vez. Vale mais que ler três vezes este texto. A ordem dos passos dentro do controller, um por um, está no [README do serviço](../README.md#6-o-caminho-de-uma-requisição).

---

## 2. O que cada pasta pode e não pode

| Pasta | Pode | Não pode |
|---|---|---|
| `resources/` | receber a requisição, conferir o schema (pelo decorator), chamar **um** controller, devolver a resposta com o status certo (201 ou 200) | saber SQL, decidir regra, ter `raise` |
| `schemas/` | dizer qual JSON é aceito na entrada, e recusar o que não é | falar com banco, decidir regra |
| `controllers/` | a ordem do caso de uso: converter o payload, chamar repositories, o connector e o motor, salvar (`commit`) | escrever consulta, saber que existe HTTP, ter regra antifraude dentro |
| `antifraud/` | a regra antifraude: os 23 sinais, os pesos, os tetos e a tabela de decisão, sobre um `Context` pronto | importar qualquer coisa de fora do próprio pacote — banco, FastAPI, `constants`, variável de ambiente, relógio |
| `repositories/` | buscar, criar e contar no banco, inclusive o histórico que o motor lê | decidir se aquilo era permitido, escolher limiar |
| `models/` | descrever as tabelas em Python | ter regra dentro |
| `dtos/` | transformar a análise gravada no dicionário que vira a resposta | buscar coisa no banco, decidir regra |
| `errors/` | definir cada erro: código, mensagem e status HTTP | ter regra de negócio dentro |
| `middlewares/` | fazer algo em **toda** requisição (identificador, log, token, tamanho do corpo, sessão de banco) | conhecer uma rota específica |
| `connectors/` | chamar um serviço de fora (o DICT): endereço, prazo, log, e traduzir qualquer falha num erro nosso | decidir o que a falha significa para o Pix, falar com o nosso banco |
| `utils/` | ferramenta de uso geral: o logger, o identificador da requisição, o documento (tirar e pôr máscara) e o leitor de schema | virar o depósito do que não se sabe onde pôr |

Um exemplo do que isso significa na prática. Em `src/antifraud/layers/decision_tree.py` você lê `if not payer.device_known: return policy.triggered("P03", ...)`. Essa frase é a regra antifraude, e ela mora no motor. Em `src/repositories/pix_history_repository.py` não existe nenhuma frase dessas: ele conta quantos Pix, com que aparelho, em que janela — e até a janela ele recebe pronta, do `policy.py`. Contar não é decidir.

O teste rápido de que uma linha está na pasta certa continua o do Base Service: **nenhum `raise` mora em `resources/`**. Quem recusa é o schema, antes, ou o controller, depois.

```bash
grep -rn "raise" src/resources/
```

Não volta nada, e isso é de propósito.

### "E a conexão com o banco, em qual pasta ela mora?"

Em duas, e a divisão é o assunto mais interessante desta seção.

Quem fala com o banco fala por uma **sessão**: um rascunho onde as mudanças ficam guardadas até alguém mandar salvar. Ela precisa nascer no início da requisição e morrer no fim, sempre — inclusive quando a rota explode no meio. Duas perguntas diferentes, então:

- **Quem cuida do ciclo de vida?** `src/middlewares/session_manager.py`. Prepara o lugar, desfaz se deu errado, fecha sempre — e **nunca salva**.
- **Quem pede a sessão?** O controller, ao ser construído. O `BaseController` chama o `get_context()` de `src/database.py`, e os repositories recebem esse contexto pronto, no construtor. A rota não escreve nada sobre banco.

O combinado que isso exige é curto, para caber na cabeça:

> **Quem chama `get_context()` é o controller, e mais ninguém.**

Motor, DTO e connector não chegam nem perto dele. O `/` e o `/health_check` não constroem controller nenhum, e por isso nunca tocam no banco: o health check que o Docker consulta a cada três segundos não depende de o banco estar de pé.

**E o `commit`?** Nunca é do middleware. Quem sabe se o trabalho terminou é o controller, e é por isso que `self.session.commit()` é a última linha antes do `return` em `src/controllers/pix_transaction_controller.py`. O middleware cuida do ciclo de vida; o controller decide o conteúdo — e, aqui, decide também quando devolver a conexão. Repare no `rollback` logo depois da primeira busca pelo `id`: ele devolve a conexão ao pool enquanto a API espera o DICT, que pode levar os 2 s do prazo — com o DICT lento, segurar a conexão durante a espera derrubava a vazão pela metade. Daí em diante quem segura é o pool de 40 threads do processo, porque cada POST prende uma delas durante a espera.

---

## 3. Onde eu mexo quando quero...

| Quero... | Mexo em | E também |
|---|---|---|
| **mudar um peso, limiar, janela ou teto** | `src/antifraud/policy.py` | a `POLICY_VERSION`, no mesmo arquivo; o teste de fronteira em `tests/integration/pix/test_fronteiras.py`; a tabela de regras do README da raiz |
| **criar uma regra nova** | a função na camada certa, em `src/antifraud/layers/`, e a linha no `CATALOG` do `policy.py` | se ela precisa de histórico novo: o campo no `Context` (`src/antifraud/contract.py`), a contagem em `src/repositories/pix_history_repository.py` e o controller que monta o `Context`. E um teste de integração |
| **mudar a ordem ou as linhas da decisão** | a `DECISION_TABLE`, no `policy.py` | a frase da linha nova em `src/antifraud/engine.py` (`_reason`) |
| **aceitar um campo novo** no JSON de entrada | `src/schemas/post_pix_transaction.json` | a conversão em `src/controllers/pix_request.py`; se o campo vai para o banco, `database/database.sql` e `src/models/` |
| **mudar o que a resposta devolve** | `src/dtos/pix_transaction_dto.py` | é o único lugar; se o campo ainda não existe no banco, antes `database/database.sql` e `src/models/` |
| **consultar o banco de outro jeito** (filtrar, contar, somar) | `src/repositories/` | o controller chama o método novo |
| **criar uma tabela** | `database/database.sql` | depois `src/models/` e o `__init__.py` da pasta, e `docker compose down -v` |
| **criar um erro novo** | `src/errors/custom_errors.py` | exportar em `src/errors/__init__.py`; o próximo código livre é o `QIT001004` |
| **trocar o mock pelo DICT de verdade** | `DICT_API_URL` e `src/connectors/dict_connector.py` | o DICT real devolve contadores, e não uma nota (Manual do DICT, cap. 18): o connector desembrulha outra resposta, e a conta da nota, que é regra, vai para a camada 3 |
| **fazer algo em toda requisição** | `src/middlewares/` | registrar em `src/app.py`, na posição certa |
| **chamar outro serviço** | `src/connectors/` | quem chama o connector é o controller, nunca o resource nem o motor |

Quatro armadilhas que pegam quase todo mundo. As duas primeiras custam caro pelo mesmo motivo: a mensagem de erro aponta para o sintoma, e não para a causa. A terceira é pior — nela não vem mensagem nenhuma.

**Renomeou uma chave de evidência?** As frases de `reasons` leem a evidência pelo nome da chave: o motivo do D-ATO-1 lê o `ratio` do P06, o do D-DRAIN lê o `attempts_5m` do P04 e o `new_destinations_10m` do P07. Renomeie a chave na camada e esqueça o `engine.py`, e o próximo Pix que cair naquela linha da tabela volta 500, com um `KeyError` no log. Os outros seguem verdes, e o defeito espera o caso raro para aparecer.

**Mexeu no `database/database.sql` e nada mudou?** Aquele arquivo roda uma vez só: **quando o banco nasce**. Depois disso o Postgres nunca mais olha para ele — nem no `docker compose up` seguinte, nem num `docker compose restart db`. O sintoma aparece longe daí, num 500 da API e numa linha do log como estas:

```
psycopg2.errors.UndefinedColumn: column "minha_coluna" does not exist
psycopg2.errors.InvalidColumnReference: there is no unique or exclusion constraint matching the ON CONFLICT specification
```

Elas não dizem "seu SQL não rodou": dizem que a coluna não existe, ou que o `UNIQUE` que o upsert procura não está lá. Dá vontade de reler o SQL procurando erro de digitação, e não tem nenhum. Para o banco nascer de novo, com o schema novo:

```bash
docker compose down -v
docker compose up -d --build --wait
```

O `-v` é o que apaga o volume, o disco do banco — e **leva junto todo Pix gravado** até aqui. Em sistema de verdade ninguém apaga o banco, claro: lá a mudança de schema entra por um comando aplicado no deploy, e é por isso que este projeto guarda o schema num arquivo versionado em vez de deixá-lo só dentro do banco.

**Mudou um peso e esqueceu a `POLICY_VERSION`?** Nada quebra, e é esse o problema. Cada análise grava a versão da política que a decidiu, e é por ela que alguém, amanhã, entende por que um Pix de ontem virou 2FA. Com a versão parada, a análise de ontem e a de hoje dizem `v0.6.0` as duas, decididas por pesos diferentes, e ninguém tem como separar uma da outra.

**Mexeu numa dependência?** As dependências moram em dois lugares, e eles não se falam. As da API são instaladas **dentro da imagem**, quando o Docker constrói a API; as dos testes ficam no **seu `.venv`**. Acrescentar uma linha no `requirements.txt` não avisa nenhum dos dois — e o `--reload` da API só recarrega o `src/`, não reinstala nada. Mexeu no `requirements.txt`: `docker compose up -d --build --wait`. Mexeu no `requirements-dev.txt`: `uv pip install -r requirements-dev.txt` de novo, com o `.venv` ativo.

E uma que não é armadilha, mas parece: **criou um arquivo e o Python diz que não existe?** Quase toda pasta tem um `__init__.py` que lista o que ela oferece; as exceções são `utils/` e `schemas/`, que se importam pelo caminho do arquivo. Abra o da pasta (por exemplo `src/repositories/__init__.py`) e acrescente a sua linha. O de `src/models/` segue a ordem das FKs: cada model vem depois dos que ele referencia.

---

## 4. A regra que explica todas as outras

A seta anda num sentido só:

```
resources → controllers → repositories → models
                 │
                 ├──→ connectors
                 └──→ antifraud
```

Um resource pode chamar um controller. Um controller pode chamar um repository, um connector e o motor. **O contrário nunca acontece** — repository não chama controller, model não sabe que existe rota.

E o `antifraud/` não importa nada de fora do próprio pacote. Duas outras peças apontam para ele, e só para ler: o DTO busca no catálogo a camada e a granularidade de cada sinal, e o repository de histórico devolve as contagens já nos tipos do `contract.py`. Nenhuma seta sai do motor.

Por que isso importa, em três respostas concretas:

- **Você acha o problema mais rápido.** Erro no formato do JSON? É `schemas/`. O Pix virou 2FA e não devia? É `antifraud/`, e o `metadata` da resposta já diz qual linha da tabela e quais sinais. Gravou o que não devia? É `controllers/`. Você já começa a procurar no lugar certo.
- **Trocar uma peça não derruba as outras.** O DICT de mentira vira o de verdade mudando o connector e uma variável de ambiente; o motor nem fica sabendo que existe HTTP.
- **Várias pessoas trabalham juntas sem colidir.** Este projeto foi escrito assim, em seções: uma no motor, outra no contrato de entrada, outra na persistência, outra no DICT, outra na suíte de testes. Arquivos diferentes, sem pisar no pé um do outro.

Quando você estiver com pressa, vai dar vontade de escrever a consulta direto no resource, ou um `if` de risco direto no controller. Funciona. E é exatamente assim que um projeto vira aquele em que ninguém mais encontra nada.

### Por que o motor é um pacote à parte

No Base Service, a regra de negócio mora no controller: um `if` que olha o status e levanta um erro. Aqui são 23 sinais, nove grupos com teto e uma tabela de decisão em ordem — no controller, isso ficaria misturado com trava, sessão e commit, e ninguém conseguiria ler a política sem ler também a transação de banco.

O motor é a parte da regra que não precisa de banco: recebe um `Context` pronto — o Pix normalizado, o histórico contado, a estatística de segurança e a marcação de fraude do DICT — e devolve uma `Analysis`. O que se ganha: a mesma entrada dá sempre a mesma análise, porque sem relógio e sem banco lá dentro a resposta não depende de que horas são nem do que outra requisição gravou no meio. Todo número que o motor usa cabe em `src/antifraud/policy.py`, com o porquê ao lado e a versão que vai gravada em cada análise. E o controller continua dono da ordem: quem busca, trava, grava e dá o commit é ele, como no Base Service.

O preço: o controller monta o `Context` inteiro antes de chamar o motor, inclusive contagens que nenhuma regra vai usar naquele Pix, e um campo novo de histórico passa por três arquivos (o `contract.py`, o repository e o controller).

E um pacote puro sem teste unitário próprio, por quê? Porque o risco não está nele. O que erra em silêncio é a contagem SQL do histórico e a montagem do `Context` — uma janela aberta no lado errado, um Pix rejeitado contado como aprovado —, e um unitário do motor, alimentado com um `Context` escrito à mão, não pegaria nenhum dos dois. Os testes de integração passam pelos três de uma vez, e é regra do time que todo teste seja assim.

### Dentro do motor, a mesma regra

As três camadas de regras (`src/antifraud/layers/`, quatro arquivos: a terceira, a do DICT, está em dois, estatística e marcação de fraude) seguem a seta por dentro: nenhuma camada importa outra, e todas importam só o `contract.py` e o `policy.py`. Quando uma regra precisa de uma condição que mora em outra camada, ela a recalcula a partir do `Context` — o R04 refaz a idade da conta de destino, que é a condição do R01, em vez de ler o sinal dele. O porquê, e o que custaria passar os sinais de uma camada para a outra, está na docstring do `Layer`, em `contract.py`.

Nenhuma camada lê o sinal de outra; quem junta é o `engine.py`, nos tetos, nas notas e na tabela de decisão.

---

## 5. Schemas e DTOs: o que entra e o que sai

Duas pastas falam de formato, e é fácil confundi-las. A divisão é a direção: **o `schemas/` cuida do que ENTRA, o `dtos/` cuida do que SAI.**

- `src/schemas/post_pix_transaction.json` descreve o JSON que o cliente manda. Quem lê isso é o `jsonschema`, acionado pelo decorator `@SchemaHandler.validate("post_pix_transaction.json")` no método do resource, antes da primeira linha dele: campo faltando, tipo errado ou campo a mais viram 400 ali mesmo. O código do decorator está em `src/utils/schema_handler.py`.

  O que um schema não tem como saber (que fevereiro não tem dia 31, que `2001:DB8::1` e `2001:db8:0:0:0:0:0:1` são o mesmo IP) fica com `src/controllers/pix_request.py`, logo depois. Ele converte o payload uma vez, e daí para frente o serviço só enxerga o `PixRequest`: datas com fuso, documento só com dígitos, IP na grafia canônica.

  **Por que um `.json`, e não uma classe Pydantic?** Nos serviços da QI Tech o contrato de entrada é um arquivo escrito em **JSON Schema**, um padrão que existe fora do Python e que quem integra com a API lê sem abrir o repositório. E aqui houve um motivo a mais: o primeiro rascunho do Pix tinha os dois, cada um com os seus limites, e a diferença entre eles virava 400 para Pix válido e 500 no INSERT. Medir em dois lugares já deu errado uma vez. O preço é o corpo chegar como dicionário: `payload["amount"]` em vez de `payload.amount`.
- `src/dtos/pix_transaction_dto.py` faz o caminho de volta. O repository entrega a linha de `pix_transaction` e as de `pix_transaction_rule_result`; o DTO devolve o `{key, status, metadata}` do contrato, com os nomes da RFC: a `pix_transaction_key` vira `key`, `payer_risk` e `recipient_risk` viram `scores`, `rule_name` vira `code`, `rule_result_data` vira `evidence`.

Abra os dois ao lado de `src/models/pix_transaction.py` e a diferença fica óbvia. Na **tabela**, o status é um número apontando para outra tabela e cada sinal é uma linha separada. Na **resposta**, tudo é um JSON só, com nome de gente. Quem faz essa travessia é o DTO, e é por isso que ele existe.

Repare que o POST e o GET passam pelo mesmo método do DTO, com as mesmas linhas lidas do banco: o POST lê de volta os sinais que acabou de gravar, em vez de usar a análise que o motor devolveu na memória. Custa duas leituras por índice a cada POST. Em troca, o corpo do POST e o do GET são o mesmo por construção.

Campo novo na resposta? Acrescente no `dtos/`. Nenhum outro arquivo precisa saber.

Uma consequência que vale conhecer: **o que o DTO monta é exatamente o que sai.** Não existe ninguém depois dele conferindo a forma — se um campo interno entrar naquele dicionário, ele vai para o cliente do mesmo jeito. Quando aparecer na resposta um dado que você não queria mostrar, comece procurando aqui.
