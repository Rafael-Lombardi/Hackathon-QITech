import os

from fastapi import Response, status
from fastapi.responses import JSONResponse

from constants import SERVICE_NAME


class HealthCheckResource:
    """As duas rotas que respondem sem token: quem é este serviço, e se ele está vivo.

    Repare no `async def`, o contrário das rotas Pix. Lá o `def` manda a
    chamada pra uma thread do pool, porque o DICT e o banco bloqueiam — a
    conta inteira está em src/resources/pix_transaction.py. Aqui não há
    espera nenhuma, e numa thread o health check entraria na mesma fila
    dos Pix: medido com 120 POSTs na fila e o DICT lento, ele levou 5,8 s
    pra responder — e o healthcheck do docker-compose.yml desiste em 5 s,
    marcando como doente uma API que estava trabalhando. Com `async def`
    ele roda direto no laço de eventos e responde em 130 ms.
    """

    async def on_get_home(self) -> JSONResponse:
        home = {"service": SERVICE_NAME, "id": str(os.getpid())}

        return JSONResponse(content=home, status_code=status.HTTP_200_OK)

    async def on_get_health_check(self) -> Response:
        # 204 quer dizer "deu tudo certo e não tenho nada a dizer".
        # Por isso a resposta não tem corpo nenhum: é um `Response`
        # pelado, e não um JSONResponse.
        return Response(status_code=status.HTTP_204_NO_CONTENT)
