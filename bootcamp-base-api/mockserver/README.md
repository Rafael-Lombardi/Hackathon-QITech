# O DICT de mentira

A camada 3 do motor pergunta ao DICT do Banco Central o quanto o recebedor é confiável. O DICT de verdade não tem essa pergunta pronta — ele devolve contadores de fraude por CPF/CNPJ (Manual Operacional do DICT, cap. 18). O time aceitou a ficção de um DICT que já responde o número, e quem faz esse papel aqui é um MockServer: o serviço `dict-mock` do `docker-compose.yml`, um servidor HTTP que devolve o que estiver combinado no `dict-expectations.json` deste diretório.

```
GET /trust-factor/{documento, só dígitos}   ->   200 {"trust_factor": 70}
```

O `trust_factor` vai de 0 a 100, e maior é mais confiável. O mock é infraestrutura de execução, como o Postgres: não tem código nosso, só o roteiro das respostas.

## O que ele responde ao subir

| Documento do recebedor | Resposta | Pra quê |
|---|---|---|
| qualquer outro | `70` | o caso comum: nada contra o destino |
| `999.999.999-99` | `12` | demonstração: abaixo do limiar de 30, o DICT01 dispara |
| `888.888.888-88` | `70`, depois de 3 s | demonstração: o DICT lento. O prazo da API é 2 s, a camada 3 sai `unavailable` e o Pix é decidido sem ela |

Por que dígitos repetidos: a Receita não emite CPF assim, então nenhuma pessoa de verdade vira "destino suspeito" num repositório público. O schema da API aceita, porque não confere dígito verificador.

O arquivo só é lido quando o mock sobe. Editou? `docker compose restart dict-mock`.

## Combinando a resposta de um documento

É assim que os testes escrevem o roteiro do seu cenário: uma expectativa por documento, com `id` igual a `trust-factor-<documento>` e prioridade 10 (a padrão tem 0, e a maior ganha).

```bash
curl -X PUT localhost:1080/mockserver/expectation -d '{
  "id": "trust-factor-12345678909",
  "priority": 10,
  "httpRequest": {"method": "GET", "path": "/trust-factor/12345678909"},
  "httpResponse": {"statusCode": 200, "body": {"trust_factor": 12}}
}'
```

Os outros cenários trocam só o `httpResponse`:

- DICT lento: `{"statusCode": 200, "body": {"trust_factor": 70}, "delay": {"timeUnit": "SECONDS", "value": 3}}`
- DICT com erro: `{"statusCode": 500}` (ou `404`)
- corpo fora do combinado: `{"statusCode": 200, "body": {"trust_factor": "alto"}}`

Pra conferir quem foi consultado, `PUT /mockserver/verify` com o mesmo `httpRequest` e `"times": {"atLeast": 1, "atMost": 1}`: 202 quer dizer que aconteceu, 406 que não. O mock aceita qualquer `INTERNAL-TOKEN`; quem quiser provar que o token foi junto põe `"headers"` no `httpRequest` do verify.

Pronto pra uso é `GET /trust-factor/<qualquer documento>` respondendo 200, uns 2 s depois de subir. O container não tem healthcheck: a imagem não tem shell pra rodar um.

## Três armadilhas, conferidas no 5.15.0

- **Mesma prioridade e sem `id`, vale a PRIMEIRA criada.** A segunda chamada pro mesmo documento não muda nada. Com o `id`, o PUT substitui a anterior.
- **`clear` por `path` leva a padrão junto**: o regex `/trust-factor/.*` dela casa com o caminho de qualquer documento. Desfaça pelo `id`: `PUT /mockserver/clear?type=EXPECTATIONS` com `{"id": "trust-factor-<documento>"}`.
- **`reset` apaga também o que veio do arquivo, e nada volta sozinho.** Tudo passa a 404, e a camada 3 fica `unavailable` em todo Pix até um `docker compose restart dict-mock`.
