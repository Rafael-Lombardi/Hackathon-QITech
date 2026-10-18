from fastapi import FastAPI

from constants import check_variables
from errors import register_error_handlers
from errors.base_error import error_verification
from middlewares import (
    register_body_size_limit_middleware,
    register_internal_token_middleware,
    register_request_context_middleware,
    register_request_logger_middleware,
    register_session_manager_middleware,
)
from resources import HealthCheckResource, PixTransactionResource
from utils.logger import setup_logging
from utils.schema_handler import load_all_validators


def create_app() -> FastAPI:
    """Monta a aplicação, peça por peça.

    Os três blocos abaixo seguem a ordem em que a requisição encontra
    cada um — de fora pra dentro:
      1. os middlewares    — o que acontece com TODA requisição
      2. as rotas          — o que a API sabe responder
      3. os error handlers — como cada erro vira uma resposta

    Essa ordem serve pra leitura, não é exigência: o FastAPI monta a
    pilha de middlewares na primeira requisição que chega, então
    registrar rota antes ou depois de middleware dá no mesmo. Só a
    ordem DENTRO do bloco de middlewares tem consequência, e ela está
    explicada logo abaixo.
    """
    # Os três None desligam a documentação automática: o FastAPI sabe
    # gerar sozinho umas páginas descrevendo a API, e aqui elas não
    # existem. O contrato de entrada é o src/schemas/post_pix_transaction.json,
    # e o README tem um exemplo pronto de cada rota.
    #
    # O redirect_slashes=False desliga um atalho do Starlette: endereço
    # com uma barra a mais no fim (GET /pix/transaction/) respondia 307,
    # mandando o cliente pra versão sem a barra — que no GET nem existe e
    # dá 405. Sem o atalho, endereço fora da tabela de rotas é 404, como
    # qualquer outro.
    application = FastAPI(
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        redirect_slashes=False,
    )

    # ────────────────────────────────────────────────────────────────
    # Os middlewares — e a ordem deles, que é regra e não gosto
    # ────────────────────────────────────────────────────────────────
    # Middleware é casca por fora da aplicação: toda requisição atravessa
    # todas na ida e na volta, e o ÚLTIMO registrado é a casca de FORA.
    # Por isso as cinco linhas abaixo estão de trás pra frente: leia de
    # baixo pra cima e a requisição faz este caminho:
    #
    #     request_context   dá um identificador único a esta requisição
    #     request_logger    escreve "ENTROU" e, na volta, "SAIU"
    #     internal_token    confere o INTERNAL-TOKEN. Sem ele, para aqui
    #     body_size_limit   recusa o POST grande demais, ou sem tamanho
    #                       declarado, antes de ler um byte do corpo
    #     session_manager   a sessão de banco: desfaz se deu errado,
    #                       fecha sempre, nunca salva
    #     as rotas
    #
    # Cada posição tem um porquê. O identificador vem antes do log, senão
    # as linhas sairiam com [-] no lugar do nome da requisição. O log vem
    # antes do token, pra que o 403 também apareça — mil deles seguidos é
    # alguém adivinhando o token. O teto do corpo vem depois do token
    # (quem não tem a chave não fica sabendo do contrato) e antes da
    # sessão (corpo recusado não tem o que pedir ao banco). A sessão é a
    # mais interna de propósito: banco é o recurso mais caro da lista, e
    # o último que se empresta. Nenhum teste guarda esta ordem — a suíte
    # fica verde com duas linhas trocadas —, e é por isso que este
    # comentário existe.
    register_session_manager_middleware(application)
    register_body_size_limit_middleware(application)
    register_internal_token_middleware(application)
    register_request_logger_middleware(application)
    register_request_context_middleware(application)

    # ────────────────────────────────────────────────────────────────
    # As rotas — o endereço, o verbo, e quem atende
    # ────────────────────────────────────────────────────────────────
    # Cada linha liga um endereço a um método de um resource. É a lista
    # COMPLETA do que esta API atende: rota que não está aqui não
    # existe, e não há um segundo lugar para procurar.
    #
    # Nos serviços da QI o framework é o Falcon, que descobre o método
    # pelo nome — chegou um POST, ele procura um `on_post`, e o registro
    # é uma linha por ENDEREÇO. O FastAPI não faz essa descoberta, então
    # aqui é uma linha por endereço E verbo. Custa mais linhas; em troca,
    # não existe rota que atenda sem estar escrita nesta lista.
    #
    # Repare no que NÃO está escrito aqui: o status de cada resposta.
    # 201 ou 200 no POST, 204 no health check — todos saem de dentro do
    # resource. É ele quem sabe se a análise nasceu agora ou já existia.
    # Esta lista diz QUEM atende cada endereço, e mais nada.
    #
    # O {external_id} da consulta é o id do payload do POST. Todo id que o
    # schema aceita cabe nesse pedaço do caminho: o schema recusa a barra,
    # que o roteador leria como outro pedaço.
    health_check_resource = HealthCheckResource()
    pix_transaction_resource = PixTransactionResource()

    application.add_api_route("/", health_check_resource.on_get_home, methods=["GET"])
    application.add_api_route(
        "/health_check",
        health_check_resource.on_get_health_check,
        methods=["GET"]
    )

    application.add_api_route(
        "/pix/transaction",
        pix_transaction_resource.on_post,
        methods=["POST"],
    )
    application.add_api_route(
        "/pix/transaction/{external_id}",
        pix_transaction_resource.on_get_by_external_id,
        methods=["GET"],
    )

    register_error_handlers(application)

    return application


def main() -> FastAPI:
    # Não deixa a API subir com configuração faltando — nem com um schema
    # de src/schemas/ quebrado: é melhor falhar agora, na hora de ligar,
    # do que na cara do cliente mais tarde.
    check_variables()
    error_verification()
    load_all_validators()
    setup_logging()

    return create_app()


app = main()
