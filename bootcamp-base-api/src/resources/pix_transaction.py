"""HTTP boundary for POST /pix/transaction."""

from fastapi import Request, status
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from dtos.pix_transaction_dto import PixTransactionInput
from errors.base_error import InvalidSchema
from errors.handlers import qi_exception_to_response
from utils.schema_handler import SchemaHandler


class PixTransactionResource:
    def __init__(self) -> None:
        pass

    async def on_post(self, request: Request) -> JSONResponse:
        try:
            payload = await request.json()
            SchemaHandler.validate(payload, "post_pix_transaction.json")
            transaction = PixTransactionInput.model_validate(payload)
        except (InvalidSchema, ValueError, ValidationError) as error:
            if isinstance(error, InvalidSchema):
                return qi_exception_to_response(error)
            return qi_exception_to_response(InvalidSchema(str(error)))

        # Contract exists; controller stays intentionally unimplemented.
        del transaction
        return JSONResponse(
            content={"detail": "Pix risk rules are not implemented"},
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
        )
