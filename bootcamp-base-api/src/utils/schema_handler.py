import functools
import json
import os

from jsonschema import Draft202012Validator, FormatChecker, ValidationError
from jsonschema.exceptions import best_match

from constants import SCHEMA_PATH
from errors import InvalidSchema


class SchemaHandler:
    """Confere o corpo da requisição contra um arquivo de src/schemas/.

    O contrato de entrada é o .json, escrito em JSON Schema: o padrão dos
    serviços da QI Tech, que existe fora do Python e que quem integra com
    a API lê sem abrir este repositório. Não há modelo Pydantic de entrada
    ao lado dele. O primeiro rascunho do Pix tinha os dois, cada um com os
    seus limites, e a diferença entre eles virava 400 pra Pix válido e 500
    no INSERT — a história está em src/controllers/pix_request.py, que é
    quem converte os tipos logo depois daqui.

    O preço é o corpo chegar como dicionário cru: `payload["amount"]`, e
    não `payload.amount`, e o editor não completa nem confere o nome do
    campo. Melhor pra quem integra, um pouco pior pra quem digita.
    """

    @staticmethod
    def validate(schema_file_name: str):
        """Decorator que valida o `payload` antes de o resource rodar.

        Usa-se assim, no método do resource que recebe corpo:

            class PixTransactionResource:
                @SchemaHandler.validate("post_pix_transaction.json")
                def on_post(self, payload: dict) -> JSONResponse:

        O nome do arquivo é o único argumento, e ele aponta pra dentro
        de src/schemas/. Método que não recebe corpo, como o GET, não leva
        decorator nenhum: não há o que conferir.

        Repare que o endereço HTTP não aparece aqui. Quem liga
        "/pix/transaction" a este método é o src/app.py, e é de propósito:
        o resource cuida do CONTEÚDO da requisição, o app.py cuida do
        ENDEREÇO dela.

        Com vários erros no mesmo payload, sai um só, o que o best_match
        do jsonschema julga mais relevante — o mais perto da raiz. O
        cliente corrige um de cada vez.
        """

        def decorator_validate(func):
            @functools.wraps(func)
            def wrapper_validate(*args, **kwargs):
                if "payload" not in kwargs:
                    raise Exception(
                        f"A rota '{func.__name__}' foi decorada com o SchemaHandler, mas nao "
                        + "tem um parametro chamado 'payload'. E de la que sai o corpo a validar."
                    )

                validator = load_validator(schema_file_name)
                error = best_match(validator.iter_errors(kwargs["payload"]))

                if error is not None:
                    raise InvalidSchema(describe_schema_error(error))

                return func(*args, **kwargs)

            return wrapper_validate

        return decorator_validate


def load_all_validators() -> None:
    """Monta, na subida da API, o validador de cada .json de src/schemas/.

    Quem chama é o main(), ao lado do check_variables: schema quebrado
    derruba a subida, em vez de virar 500 no primeiro POST.
    """
    for schema_file_name in sorted(os.listdir(SCHEMA_PATH)):
        if schema_file_name.endswith(".json"):
            load_validator(schema_file_name)


@functools.cache
def load_validator(schema_file_name: str) -> Draft202012Validator:
    """O validador de um schema, montado uma vez e guardado.

    O arquivo não muda com a API no ar, e montar o validador é a parte
    cara: ler o disco e conferir o próprio schema contra o metaschema do
    JSON Schema. O validate() solto do jsonschema refaz as duas coisas a
    cada chamada. Medido com o post_pix_transaction.json, dentro do
    container e com a máquina ociosa: 5 ms por POST daquele jeito, 0,18 ms
    com o validador guardado.

    O format_checker é o que dá dente ao `format` do schema. Sem ele,
    `format` é só anotação, e o ipv4/ipv6 de source.ip deixaria passar
    qualquer texto.

    Repare que é UM validador pra todas as requisições ao mesmo tempo — o
    resource roda em threads. Ao seguir um $ref, ele anota por onde está
    passando, e essa anotação é dividida entre as threads. Enquanto todo
    $ref apontar pra dentro do próprio arquivo ("#/$defs/..."), como hoje,
    isso não muda resposta nenhuma: 32 threads, 40 mil validações, zero
    divergências. Schema com $ref pra fora pede um validador por
    requisição, a 0,1 ms a mais.
    """
    schema_path = os.path.join(SCHEMA_PATH, schema_file_name)

    with open(schema_path, encoding="utf-8") as schema_file:
        schema = json.load(schema_file)

    # Schema torto quebra aqui, alto e com o nome do erro, em vez de
    # validar errado em silêncio.
    Draft202012Validator.check_schema(schema)

    return Draft202012Validator(schema, format_checker=FormatChecker())


def describe_schema_error(error: ValidationError) -> str:
    """Diz o que estava errado e ONDE, quando o campo é aninhado.

    O jsonschema já escreve uma boa mensagem ("'id' is a required
    property"), mas ela não diz em que parte do JSON o problema está. Pra
    um campo na raiz isso não faz falta; pra um campo aninhado, faz toda:
    "'participant' was unexpected" sozinho não diz se o intruso estava na
    conta de origem ou na de destino. Com o caminho, sai "... in
    source_account".
    """
    location = []
    for part in error.absolute_path:
        location.append(str(part))

    if location:
        return f"{error.message} in {'.'.join(location)}"

    return error.message
