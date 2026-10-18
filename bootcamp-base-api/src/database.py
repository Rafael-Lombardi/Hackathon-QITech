from contextvars import ContextVar
from typing import Optional

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from constants import DATABASE_URL

# Tests that import a controller directly do not pass through ``app.main``;
# fail with the same actionable message instead of SQLAlchemy's opaque
# ``Expected string or URL object, got None``.
if not DATABASE_URL:
    raise EnvironmentError(
        "DATABASE_URL não está configurada. Copie .env.example para .env "
        "ou exporte DATABASE_URL antes de rodar a API/testes."
    )

# hide_parameters: sem ele, todo erro de banco traz os parâmetros do
# comando na mensagem, e o log da exceção imprimiria o CPF inteiro — que,
# pela LGPD, nunca vai pro log.
engine = create_engine(DATABASE_URL, pool_size=5, pool_recycle=600, pool_pre_ping=True, hide_parameters=True)
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
