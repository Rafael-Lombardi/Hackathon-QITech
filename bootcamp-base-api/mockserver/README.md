# O DICT de mentira

O DICT é o Diretório de Identificadores de Contas Transacionais, o cadastro do Banco Central que liga chave Pix a conta. O de verdade devolve contadores de fraude por CPF/CNPJ (Manual Operacional do DICT, cap. 18); o time aceitou a ficção de um DICT que já responde a nota. A camada 3 do motor pergunta a ele quão confiável é o recebedor:

```
GET /trust-factor/{documento, só dígitos}   ->   200 {"trust_factor": 70}
```

O `trust_factor` vai de 0 a 100, e maior é mais confiável.

Quem faz o papel do DICT é um [MockServer](https://www.mock-server.com): o serviço `dict-mock` do `docker-compose.yml`, um servidor HTTP que responde o que estiver combinado com ele. Cada combinado é uma *expectativa*: o pedido que ela reconhece (`httpRequest`) e a resposta que ela dá (`httpResponse`). As do `dict-expectations.json`, deste diretório, sobem com ele. A mesma porta, 1080, faz os dois papéis: responde como DICT em `/trust-factor/...` e recebe os combinados em `/mockserver/...`.

Ele conta como infraestrutura de execução, como o Postgres, e não como mais uma API nossa. O desafio deixa "outras APIs" de fora, e o mock não tem código nosso, só o roteiro das respostas. Um FastAPI nosso fazendo o papel do DICT, a alternativa, seria uma segunda API.

Depois de subir, ele leva alguns segundos pra atender: de 2 a 18 s nas nossas medições, conforme a carga da máquina. Pronto é `GET /trust-factor/<qualquer documento>` respondendo 200. O container não tem healthcheck, porque a imagem não tem shell pra rodar um.

## O que ele responde ao subir

| Expectativa (`id`) | Documento do recebedor | Resposta | Pra quê |
|---|---|---|---|
| `trust-factor-99999999999` | `999.999.999-99` | `12` | demonstração: abaixo do limiar de 30, o DICT01 dispara |
| `trust-factor-88888888888` | `888.888.888-88` | `70`, depois de 3 s | demonstração: o DICT lento. O prazo da API é 2 s, a camada 3 sai `unavailable` e o Pix é decidido sem ela. Com `DICT_API_TIMEOUT` de 3 s ou mais, a resposta chega a tempo e a demonstração some |
| `trust-factor-default` | qualquer outro | `70` | o caso comum: nada contra o destino |

Por que dígitos repetidos: a Receita não emite CPF assim, então nenhuma pessoa de verdade aparece como destino suspeito num repositório público. O schema da API aceita, porque não confere dígito verificador.

O arquivo só é lido quando o mock sobe: editou, `docker compose restart dict-mock`. O restart também apaga tudo o que os testes combinaram, e é por ele que se começa uma demonstração.

## Combinando a resposta de um documento

É assim que os testes escrevem o roteiro de cada cenário: uma expectativa por documento, com `id` igual a `trust-factor-<documento>`, prioridade 10 (a padrão tem 0, e a maior ganha) e validade de 5 minutos.

```bash
curl -X PUT localhost:1080/mockserver/expectation -d '{
  "id": "trust-factor-12345678909",
  "priority": 10,
  "timeToLive": {"timeUnit": "MINUTES", "timeToLive": 5},
  "httpRequest": {"method": "GET", "path": "/trust-factor/12345678909"},
  "httpResponse": {"statusCode": 200, "body": {"trust_factor": 12}}
}'
```

Os outros cenários trocam só o `httpResponse`:

- DICT lento: `{"statusCode": 200, "body": {"trust_factor": 70}, "delay": {"timeUnit": "SECONDS", "value": 3}}`
- DICT com erro: `{"statusCode": 500}` (ou `404`)
- corpo fora do combinado: `{"statusCode": 200, "body": {"trust_factor": "alto"}}`

Pra conferir quem foi consultado, `PUT /mockserver/verify` com o mesmo `httpRequest` e `"times": {"atLeast": 1, "atMost": 1}`. 202 quer dizer que aconteceu; 406, que não.

O mock aceita qualquer `INTERNAL-TOKEN`. Pra provar que o token foi junto, o `httpRequest` do verify leva `"headers": {"INTERNAL-TOKEN": ["default_token"]}`.

## Quatro armadilhas, conferidas no 5.15.0

- **Com a mesma prioridade e sem `id`, vale a primeira criada.** A segunda chamada pro mesmo documento não muda nada. Com o `id`, o PUT substitui a anterior.
- **`PUT /mockserver/clear` por `path` leva a padrão junto.** O regex `/trust-factor/.*` dela casa com o caminho de qualquer documento. Desfaça pelo `id`: `PUT /mockserver/clear?type=EXPECTATIONS` com `{"id": "trust-factor-<documento>"}`.
- **`PUT /mockserver/reset` apaga também o que veio do arquivo, e nada volta sozinho.** Tudo passa a 404, e a camada 3 fica `unavailable` em todo Pix até um `docker compose restart dict-mock`.
- **O mock guarda no máximo 5.000 expectativas.** Quando enche, descarta as mais antigas, seja qual for a prioridade — e as mais antigas são as do arquivo, carregadas na subida. Todo documento sem expectativa própria passa a 404: DICT01 `unavailable` em todo Pix, sem aviso do mock. É pra isso a validade de 5 minutos: vencida, a expectativa sai na próxima consulta ao mock e libera a vaga.
