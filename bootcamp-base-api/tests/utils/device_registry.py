"""Seed do cadastro autoritativo de dispositivos para cenários.

Esse seed é deliberadamente SQL, separado de ``seed`` de Pix: cadastro é
uma fonte externa/autoritativa e não um evento observado do histórico.
"""

from datetime import datetime
from os import environ
from uuid import uuid4

from sqlalchemy import create_engine, text


def seed_devices(devices: list[dict]) -> None:
    if not devices:
        return
    database_url = environ.get(
        "DATABASE_URL",
        "postgresql+psycopg2://bootcamp:bootcamp@localhost:5432/bootcamp",
    )
    engine = create_engine(database_url)
    with engine.begin() as connection:
        for device in devices:
            account_id = connection.execute(
                text(
                    """
                    INSERT INTO account
                        (account_key, document_number, participant, branch, account_number, opening_date)
                    VALUES (:key, :document, NULL, '0001', :account_number, :registered_at)
                    ON CONFLICT (participant, branch, account_number, document_number)
                    DO UPDATE SET document_number = EXCLUDED.document_number
                    RETURNING id
                    """
                ),
                {"key": str(uuid4()), "document": device["payer_document"],
                 "account_number": "device-seed-" + device["payer_document"][-8:],
                 "registered_at": device["registered_at"]},
            ).scalar_one()
            connection.execute(
                text(
                    """
                    INSERT INTO device_registry
                        (account_id, device_id, registered_at, revoked_at, provenance)
                    VALUES (:account_id, :device_id, :registered_at, :revoked_at, :provenance)
                    ON CONFLICT (account_id, device_id) DO UPDATE SET
                        registered_at = EXCLUDED.registered_at,
                        revoked_at = EXCLUDED.revoked_at,
                        provenance = EXCLUDED.provenance
                    """
                ),
                {
                    "account_id": account_id,
                    "device_id": device["device_id"],
                    "registered_at": device["registered_at"],
                    "revoked_at": device.get("revoked_at"),
                    "provenance": device.get("provenance", "scenario"),
                },
            )
    engine.dispose()
