"""Seeds reproduzíveis para restrições e inteligência externa.

Esses dados ficam separados de ``seed`` de Pix: são fontes autoritativas
observadas pelo motor, não eventos que devem alterar o histórico.
"""

from os import environ
from uuid import uuid4

from sqlalchemy import create_engine, text


def _engine():
    return create_engine(
        environ.get(
            "DATABASE_URL",
            "postgresql+psycopg2://bootcamp:bootcamp@localhost:5432/bootcamp",
        )
    )


def _customer(connection, document: str) -> int:
    return connection.execute(
        text(
            """
            INSERT INTO customer (customer_key, document_number)
            VALUES (:key, :document)
            ON CONFLICT (document_number) DO UPDATE SET document_number = EXCLUDED.document_number
            RETURNING id
            """
        ),
        {"key": str(uuid4()), "document": document},
    ).scalar_one()


def seed_restrictions(restrictions: list[dict]) -> None:
    """Insere ou atualiza restrições de cenário por ``occurrence_id``.

    Formatos aceitos: ``payer_document`` para ``payer``,
    ``recipient_document`` para ``recipient_holder`` e ``account`` com
    ``branch``, ``account_number`` e opcionalmente ``participant`` para
    ``recipient_account``.
    """
    if not restrictions:
        return
    engine = _engine()
    with engine.begin() as connection:
        for restriction in restrictions:
            scope = restriction["scope"]
            document = restriction.get("payer_document") or restriction.get("recipient_document")
            customer_id = _customer(connection, document) if document else None
            account_id = None
            account = restriction.get("account")
            if scope == "recipient_account":
                if not account or not restriction.get("recipient_document"):
                    raise ValueError("recipient_account exige recipient_document e account")
                customer_id = _customer(connection, restriction["recipient_document"])
                account_id = connection.execute(
                    text(
                        """
                        SELECT id FROM account
                        WHERE customer_id = :customer_id
                          AND participant IS NOT DISTINCT FROM :participant
                          AND branch = :branch AND account_number = :account_number
                        LIMIT 1
                        """
                    ),
                    {
                        "customer_id": customer_id,
                        "participant": account.get("participant"),
                        "branch": account["branch"],
                        "account_number": account["account_number"],
                    },
                ).scalar()
                if account_id is None:
                    account_id = connection.execute(
                        text(
                            """
                            INSERT INTO account
                                (account_key, customer_id, participant, branch, account_number, opening_date)
                            VALUES (:key, :customer_id, :participant, :branch, :account_number, :opening_date)
                            RETURNING id
                            """
                        ),
                        {
                            "key": str(uuid4()),
                            "customer_id": customer_id,
                            "participant": account.get("participant"),
                            "branch": account["branch"],
                            "account_number": account["account_number"],
                            "opening_date": restriction["active_from"],
                        },
                    ).scalar_one()
            connection.execute(
                text(
                    """
                    INSERT INTO risk_restriction
                        (scope, customer_id, account_id, active_from, active_until,
                         status, provenance, occurrence_id)
                    VALUES (:scope, :customer_id, :account_id, :active_from, :active_until,
                            :status, :provenance, :occurrence_id)
                    ON CONFLICT (occurrence_id) DO UPDATE SET
                        scope = EXCLUDED.scope,
                        customer_id = EXCLUDED.customer_id,
                        account_id = EXCLUDED.account_id,
                        active_from = EXCLUDED.active_from,
                        active_until = EXCLUDED.active_until,
                        status = EXCLUDED.status,
                        provenance = EXCLUDED.provenance
                    """
                ),
                {
                    "scope": scope,
                    "customer_id": customer_id,
                    "account_id": account_id,
                    "active_from": restriction["active_from"],
                    "active_until": restriction.get("active_until"),
                    "status": restriction.get("status", "active"),
                    "provenance": restriction.get("provenance", "scenario"),
                    "occurrence_id": restriction.get("occurrence_id", str(uuid4())),
                },
            )
    engine.dispose()


def seed_external_risk(assessments: list[dict]) -> None:
    """Insere snapshots ``clear``, ``suspicious`` ou ``confirmed_in_mock``."""
    if not assessments:
        return
    engine = _engine()
    with engine.begin() as connection:
        for assessment in assessments:
            customer_id = _customer(connection, assessment["customer_document"])
            connection.execute(
                text(
                    """
                    INSERT INTO external_risk_assessment
                        (customer_id, axis, assessment, valid_from, valid_until,
                         provenance, occurrence_id, severity, source_version, observed_at)
                    VALUES (:customer_id, :axis, :assessment, :valid_from, :valid_until,
                            :provenance, :occurrence_id, :severity, :source_version, :observed_at)
                    ON CONFLICT (occurrence_id) DO UPDATE SET
                        customer_id = EXCLUDED.customer_id,
                        axis = EXCLUDED.axis,
                        assessment = EXCLUDED.assessment,
                        valid_from = EXCLUDED.valid_from,
                        valid_until = EXCLUDED.valid_until,
                        provenance = EXCLUDED.provenance,
                        severity = EXCLUDED.severity,
                        source_version = EXCLUDED.source_version,
                        observed_at = EXCLUDED.observed_at
                    """
                ),
                {
                    "customer_id": customer_id,
                    "axis": assessment["axis"],
                    "assessment": assessment["assessment"],
                    "valid_from": assessment["valid_from"],
                    "valid_until": assessment.get("valid_until"),
                    "provenance": assessment.get("provenance", "scenario"),
                    "occurrence_id": assessment.get("occurrence_id", str(uuid4())),
                    "severity": assessment.get("severity"),
                    "source_version": assessment.get("source_version"),
                    "observed_at": assessment.get("observed_at"),
                },
            )
    engine.dispose()
