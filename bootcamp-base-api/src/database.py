from contextvars import ContextVar
from typing import Optional

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from constants import DATABASE_URL

# hide_parameters: sem ele, todo erro de banco traz os parâmetros do
# comando na mensagem, e o log da exceção imprimiria o CPF inteiro — que,
# pela LGPD, nunca vai pro log.
#
# Os tetos: sem eles, uma consulta travada prende a thread e a conexão
# pra sempre, e quem espera conexão no pool espera 30 s pra falhar. A
# decisão do Pix tem de sair em segundos — quem não conseguir conexão em
# 5 s, ou não terminar um comando em 10 s, falha com nome; e uma transação
# esquecida aberta é derrubada pelo banco em 30 s.
engine = create_engine(
    DATABASE_URL,
    pool_size=5,
    pool_timeout=5,
    pool_recycle=600,
    pool_pre_ping=True,
    hide_parameters=True,
    connect_args={"options": "-c statement_timeout=10000 -c idle_in_transaction_session_timeout=30000"},
)
SessionLocal = sessionmaker(bind=engine, autoflush=False)


class Context:
    def __init__(self) -> None:
        self.db_session: Optional[Session] = None

    def get_or_create_session(self) -> Session:
        if self.db_session is None:
            self.db_session = SessionLocal()
        return self.db_session


_context: ContextVar[Optional[Context]] = ContextVar("context", default=None)


def open_context() -> Context:
    context = Context()
    _context.set(context)
    return context


def clear_context() -> None:
    _context.set(None)


def get_context() -> Context:
    context = _context.get()
    if context is None:
        raise RuntimeError("Request context is not open")
    return context
