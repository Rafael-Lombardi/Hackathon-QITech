from fastapi import FastAPI, Request

from database import clear_context, open_context


def register_session_manager_middleware(application: FastAPI) -> None:
    # Um contexto por requisição; quem commita é o controller.
    @application.middleware("http")
    async def manage_session(request: Request, call_next):
        context = open_context()
        try:
            return await call_next(request)
        except Exception:
            if context.db_session is not None:
                context.db_session.rollback()
            raise
        finally:
            if context.db_session is not None:
                context.db_session.close()
            clear_context()
