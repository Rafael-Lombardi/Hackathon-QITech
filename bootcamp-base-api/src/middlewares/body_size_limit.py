from fastapi import FastAPI, Request

from errors.base_error import InvalidSchema
from errors.handlers import qi_exception_to_response

# O teto do corpo da requisição: 16 KiB, quase quatro vezes o maior Pix
# válido — 4,2 KiB, com device_id e channel cheios de emoji escrito como
# \uXXXX. Sem teto, um corpo de 13 MB levou 4 s e 180 MB de memória pra
# ser recusado, e segurou por 3,7 s um Pix pequeno que chegou junto.
MAX_BODY_BYTES = 16 * 1024


def register_body_size_limit_middleware(application: FastAPI) -> None:
    """Recusa com 400 o POST de corpo grande demais, sem ler byte nenhum dele.

    A conta é feita só pelo Content-Length, e por isso ele é obrigatório:
    POST sem ele — o corpo mandado em pedaços, sem tamanho declarado —
    também volta 400. No HTTP isso tem código próprio, o 411 Length
    Required; aqui ele vira o nosso 400 de contrato. curl, Postman e o
    requests do Python mandam o header sozinhos em todo corpo comum: só
    sai sem ele o corpo montado aos pedaços de propósito. E é exigir o
    tamanho de antemão que poupa este middleware de ler o corpo inteiro
    pra contar, e de devolvê-lo depois pra rota ler de novo.

    Como no internal_token, a recusa é DEVOLVIDA, e não levantada: aqui
    fora ninguém escuta exceção pra traduzir em resposta.
    """

    @application.middleware("http")
    async def limit_body_size(request: Request, call_next):
        if request.method != "POST":
            return await call_next(request)

        declared_length = request.headers.get("content-length")

        if declared_length is None:
            return qi_exception_to_response(InvalidSchema("Request body must declare its Content-Length"))

        # O int() não tem try: Content-Length que não é número o servidor
        # HTTP recusa antes de chegar aqui.
        if int(declared_length) > MAX_BODY_BYTES:
            return qi_exception_to_response(InvalidSchema(f"Request body is larger than {MAX_BODY_BYTES} bytes"))

        return await call_next(request)
