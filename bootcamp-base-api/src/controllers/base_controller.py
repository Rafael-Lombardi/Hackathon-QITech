from abc import ABCMeta

from database import get_context
from utils.logger import get_logger


class BaseController(metaclass=ABCMeta):
    """O que todo controller tem em comum: o contexto da requisição, a sessão e o log.

    Nada chega por parâmetro. O controller pega o contexto que o
    middleware de sessão abriu pra esta requisição — e é o ÚNICO lugar do
    projeto que chama `get_context()`. Repository recebe o contexto pronto
    no construtor, e motor, DTO e connector não chegam nem perto dele.

    A sessão nasce aqui, no `get_or_create_session`: construir um
    controller é dizer "eu uso banco". O commit também é dele, na última
    linha antes do return — nem o middleware nem os repositories salvam
    nada.
    """

    def __init__(self, class_name: str) -> None:
        self.context = get_context()
        self.session = self.context.get_or_create_session()
        self.logger = get_logger(class_name)
