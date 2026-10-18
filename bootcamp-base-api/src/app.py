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
    # O redirect_slashes=False desliga um atalho do Starlette: endereço
    # com uma barra a mais no fim (GET /pix/transaction/) respondia 307,
    # mandando o cliente pra versão sem a barra — que no GET nem existe e
    # dá 405. Sem o atalho, endereço fora da tabela de rotas é 404, como
    # qualquer outro.
    application = FastAPI(
        title="Antifraude Pix API",
        description="Motor determinístico de decisão antifraude Pix. Use INTERNAL-TOKEN nas rotas de transação.",
        version="1.0.0",
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
        redirect_slashes=False,
    )

    # ────────────────────────────────────────────────────────────────
    # Os middlewares — e a ordem deles, que é regra e não gosto
    # ────────────────────────────────────────────────────────────────
    # Middleware é uma camada por fora da aplicação: toda requisição
    # atravessa todas elas na ida, e todas de novo na volta. São como
    # cascas de cebola, e o ÚLTIMO registrado é a casca de FORA.
    #
    # Ou seja: destas cinco linhas, a de BAIXO é a primeira que a
    # requisição encontra, e a de CIMA é a última. Lendo de fora para
    # dentro, acontece isto:
    #
    #     requisição chega
    #           ↓
    #     request_context   dá um identificador único a esta requisição
    #           ↓
    #     request_logger    escreve "ENTROU" e, na volta, "SAIU"
    #           ↓
    #     internal_token    confere o INTERNAL-TOKEN. Sem ele, para aqui
    #           ↓
    #     body_size_limit   recusa o POST de corpo grande demais, ou sem
    #           ↓           tamanho declarado, antes de ler um byte dele
    #           ↓
    #     session_manager   cuida da sessão de banco: desfaz se deu
    #           ↓           errado, fecha sempre — e nunca salva
    #           ↓
    #     as rotas
    #
    # Cada posição tem um porquê, e trocar duas linhas muda o que a API
    # faz:
    #
    # • O identificador vem ANTES do log: se viesse depois, as linhas de
    #   log da requisição sairiam sem o nome dela, e o middleware
    #   existiria para nada.
    # • O log vem ANTES do token: assim a tentativa recusada com 403
    #   também aparece no log — e essa é justamente a que você quer ver,
    #   porque mil delas seguidas é alguém tentando adivinhar o token.
    # • A sessão de banco fica por DENTRO do token, encostada nas
    #   rotas: quem não passou na porta leva 403 na camada de cima e não
    #   chega a ter uma sessão. É o mais interno de propósito — banco é
    #   o recurso mais caro desta lista, e o último que se empresta.
    # • O teto do corpo fica entre os dois. Depois do token, porque quem
    #   nem tem a chave leva 403 e não fica sabendo nada do contrato. E
    #   antes da sessão, porque um corpo recusado não tem o que pedir ao
    #   banco.
    #
    # Nada disso é teoria, e dá pra ver com os olhos. Troque as duas
    # últimas linhas de lugar (o identificador passa a ser registrado
    # antes do log, ou seja, a executar DEPOIS dele), consulte um Pix
    # pelo GET e olhe o log:
    #
    #     docker compose logs api | grep request_logger | tail -2
    #
    # (O grep é necessário: sem ele o tail pega a linha de acesso do
    # uvicorn, que é outra coisa e não passa por este middleware. E o
    # /health_check não serve pro teste: ele é das rotas que o
    # request_logger deixa de fora.)
    #
    # As duas linhas saem com [-] no lugar do identificador: o log
    # aconteceu antes de existir um nome para aquela requisição. Desfaça
    # a troca e o nome volta.
    #
    # Repare no que essa experiência tem de incômodo: a suíte continua
    # TODA VERDE com a ordem trocada. Nenhum teste aqui guarda esta
    # ordem — o preço aparece no dia do incidente, quando o log não
    # servir pra achar a requisição. Teste que não existe não avisa
    # nada, e é por isso que este comentário existe.
    #
    # ATENÇÃO, e é aqui que quase todo mundo tropeça: as cinco linhas
    # abaixo são o DIAGRAMA ACIMA DE TRÁS PRA FRENTE. O FastAPI embrulha
    # a aplicação de dentro pra fora, então o ÚLTIMO registrado é o
    # PRIMEIRO a executar. Leia de baixo pra cima e o diagrama volta.
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
