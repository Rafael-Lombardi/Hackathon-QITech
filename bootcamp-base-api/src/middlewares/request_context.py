from fastapi import FastAPI, Request

from utils.request_context import REQUEST_ID_HEADER, build_request_id, set_request_id


def register_request_context_middleware(application: FastAPI) -> None:
    @application.middleware("http")
    async def create_request_context(request: Request, call_next):
        request_id = build_request_id(request.headers.get(REQUEST_ID_HEADER))
        set_request_id(request_id)
        response = await call_next(request)
        response.headers[REQUEST_ID_HEADER] = request_id
        return response
