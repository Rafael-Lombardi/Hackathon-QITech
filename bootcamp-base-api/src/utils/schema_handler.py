"""Runtime JSON Schema validation at HTTP boundary."""

import json
import os

from jsonschema import FormatChecker, ValidationError, validate

from constants import SCHEMA_PATH
from errors.base_error import InvalidSchema


class SchemaHandler:
    @staticmethod
    def validate(payload: dict, schema_name: str) -> None:
        path = os.path.join(SCHEMA_PATH, schema_name)
        with open(path, encoding="utf-8") as schema_file:
            schema = json.load(schema_file)
        try:
            validate(payload, schema, format_checker=FormatChecker())
        except ValidationError as error:
            raise InvalidSchema(error.message) from error
