from datetime import datetime, timezone
from os import environ

from fastapi import Request, status as http_status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from controllers import PixTransactionController
from utils.schema_handler import SchemaHandler


class PixTransactionResource:
    """A porta de entrada HTTP do Pix: o POST que analisa e o GET que consulta.

    Um método daqui faz três coisas, nesta ordem, e nada além disso:
    confere o corpo contra o schema, chama o controller e devolve o que
    ele respondeu, com o status certo. Nenhuma regra, nenhum SQL. Um
    método daqui cabe em cinco linhas; quando não couber, é sinal de que
    uma regra vazou da camada de baixo pra cá.

    O 400 do schema nasce no decorator, antes da primeira linha do
    on_post. Os outros erros (404, 409) também não viram resposta aqui:
    sobem como QIException, e quem traduz é o src/errors/handlers.py.

    ────────────────────────────────────────────────────────────────
    POR QUE `def`, E NÃO `async def`
    ────────────────────────────────────────────────────────────────
    O controller espera o DICT, até os 2 s do prazo, e espera o banco. As
    duas esperas BLOQUEIAM: nem o requests nem o SQLAlchemy daqui sabem
    esperar de outro jeito. Numa rota `async def`, o FastAPI roda o
    método no próprio laço de eventos — a fila única por onde passam
    todas as requisições —, e uma espera dessas trava a fila inteira. A
    API toda fica surda enquanto o DICT não responde. Medido com o DICT
    lento: durante um POST, o /health_check levou 2 s com a rota
    `async def` e 3 ms com `def`.

    Com `def`, o FastAPI manda cada chamada pra uma thread de um pool de
    40, e a espera prende só aquela thread. O preço é esse número: com 40
    requisições esperando ao mesmo tempo, a 41ª espera uma thread vagar.
    O /health_check fica fora dessa fila de propósito, e por isso é
    `async def` (src/resources/health_check.py).

    ────────────────────────────────────────────────────────────────
    201 OU 200 — O STATUS É DECIDIDO AQUI
    ────────────────────────────────────────────────────────────────
    201 quando a análise nasceu nesta requisição; 200 quando o mesmo Pix
    chegou de novo e a resposta é a análise que já estava gravada. Quem
    sabe qual dos dois aconteceu é o controller, e ele diz; quem traduz
    em HTTP é este arquivo.
    """

    @SchemaHandler.validate("post_pix_transaction.json")
    def on_post(self, payload: dict, request: Request) -> JSONResponse:
        controller = PixTransactionController()
        test_received_at = None
        if environ.get("APP_ENV", "local") in {"local", "test"}:
            raw_clock = request.headers.get("X-Test-Received-At")
            if raw_clock:
                try:
                    test_received_at = datetime.fromisoformat(raw_clock).astimezone(timezone.utc)
                except ValueError:
                    test_received_at = None
        pix_transaction, created = controller.create(payload, test_received_at=test_received_at)

        status_code = http_status.HTTP_201_CREATED if created else http_status.HTTP_200_OK

        return JSONResponse(content=jsonable_encoder(pix_transaction), status_code=status_code)

    def on_get_by_external_id(self, external_id: str) -> JSONResponse:
        controller = PixTransactionController()
        pix_transaction = controller.get_by_external_id(external_id)

        return JSONResponse(content=jsonable_encoder(pix_transaction), status_code=http_status.HTTP_200_OK)
