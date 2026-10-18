from fastapi import status as http_status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from controllers import PixTransactionController
from utils.schema_handler import SchemaHandler


class PixTransactionResource:
    """A porta de entrada HTTP do Pix: o POST que analisa e o GET que consulta.

    Um método daqui confere o corpo contra o schema, chama o controller e
    devolve o que ele respondeu, com o status certo. Nenhuma regra, nenhum
    SQL: quando um método não couber em cinco linhas, uma regra vazou pra
    cá. O 400 do schema nasce no decorator; 404 e 409 sobem como
    QIException e viram resposta em src/errors/handlers.py.

    `def`, e não `async def`: o controller espera o DICT (até 2 s) e o
    banco, e as duas esperas bloqueiam. Numa rota async isso travaria o
    laço de eventos inteiro — medido: o /health_check levou 2 s durante um
    POST com o DICT lento, contra 3 ms com `def`. Com `def` cada chamada
    vai pra uma thread do pool de 40, e só ela espera. O /health_check é
    `async def` justamente pra ficar fora dessa fila.

    201 quando a análise nasceu nesta requisição, 200 quando o mesmo Pix
    chegou de novo: o controller diz qual dos dois, e este arquivo traduz.
    """

    @SchemaHandler.validate("post_pix_transaction.json")
    def on_post(self, payload: dict) -> JSONResponse:
        controller = PixTransactionController()
        pix_transaction, created = controller.create(payload)

        status_code = http_status.HTTP_201_CREATED if created else http_status.HTTP_200_OK

        return JSONResponse(content=jsonable_encoder(pix_transaction), status_code=status_code)

    def on_get_by_external_id(self, external_id: str) -> JSONResponse:
        controller = PixTransactionController()
        pix_transaction = controller.get_by_external_id(external_id)

        return JSONResponse(content=jsonable_encoder(pix_transaction), status_code=http_status.HTTP_200_OK)
