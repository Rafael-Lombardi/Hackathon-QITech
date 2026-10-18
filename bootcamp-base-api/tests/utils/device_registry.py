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
            customer_id = connection.execute(
                text(
                    """
                    INSERT INTO customer (customer_key, document_number)
                    VALUES (:key, :document)
                    ON CONFLICT (document_number) DO UPDATE
                        SET document_number = EXCLUDED.document_number
                    RETURNING id
                    """
                ),
                {"key": str(uuid4()), "document": device["payer_document"]},
            ).scalar_one()
            connection.execute(
                text(
                    """
                    INSERT INTO device_registry
                        (customer_id, device_id, registered_at, revoked_at, provenance)
                    VALUES (:customer_id, :device_id, :registered_at, :revoked_at, :provenance)
                    ON CONFLICT (customer_id, device_id) DO UPDATE SET
                        registered_at = EXCLUDED.registered_at,
                        revoked_at = EXCLUDED.revoked_at,
                        provenance = EXCLUDED.provenance
                    """
                ),
                {
                    "customer_id": customer_id,
                    "device_id": device["device_id"],
                    "registered_at": device["registered_at"],
                    "revoked_at": device.get("revoked_at"),
                    "provenance": device.get("provenance", "scenario"),
                },
            )
    engine.dispose()
