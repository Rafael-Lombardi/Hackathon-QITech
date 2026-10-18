"""Seeds reproduzíveis para restrições e evidência antifraude do DICT.

Esses dados ficam separados de ``seed`` de Pix: são fontes autoritativas
observadas pelo motor, não eventos que devem alterar o histórico.
"""

from os import environ
from uuid import uuid4
from datetime import datetime, timezone

from sqlalchemy import create_engine, text


def _engine():
    return create_engine(
        environ.get(
            "DATABASE_URL",
            "postgresql+psycopg2://bootcamp:bootcamp@localhost:5432/bootcamp",
        )
    )


def _account(connection, document: str, account: dict | None = None, opening_date=None) -> int:
    account = account or {"participant": None, "branch": "0001", "account_number": "seed-" + document[-8:]}
    opening_date = opening_date or datetime.now(timezone.utc)
    return connection.execute(
        text(
            """
            INSERT INTO account
                (account_key, document_number, participant, branch, account_number, opening_date)
            VALUES (:key, :document, :participant, :branch, :account_number, :opening_date)
            ON CONFLICT (participant, branch, account_number, document_number)
            DO UPDATE SET document_number = EXCLUDED.document_number
            RETURNING id
            """
        ),
        {"key": str(uuid4()), "document": document,
         "participant": account.get("participant"), "branch": account["branch"],
         "account_number": account["account_number"], "opening_date": opening_date},
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
            account_id = _account(connection, document) if document else None
            account = restriction.get("account")
            if scope == "recipient_account":
                if not account or not restriction.get("recipient_document"):
                    raise ValueError("recipient_account exige recipient_document e account")
                account_id = _account(connection, restriction["recipient_document"], account, restriction["active_from"])
            connection.execute(
                text(
                    """
                    INSERT INTO risk_restriction
                        (scope, account_id, active_from, active_until,
                         status, provenance, occurrence_id)
                    VALUES (:scope, :account_id, :active_from, :active_until,
                            :status, :provenance, :occurrence_id)
                    ON CONFLICT (occurrence_id) DO UPDATE SET
                        scope = EXCLUDED.scope,
                        account_id = EXCLUDED.account_id,
                        active_from = EXCLUDED.active_from,
                        active_until = EXCLUDED.active_until,
                        status = EXCLUDED.status,
                        provenance = EXCLUDED.provenance
                    """
                ),
                {
                    "scope": scope,
                    "account_id": account_id,
                    "active_from": restriction["active_from"],
                    "active_until": restriction.get("active_until"),
                    "status": restriction.get("status", "active"),
                    "provenance": restriction.get("provenance", "scenario"),
                    "occurrence_id": restriction.get("occurrence_id", str(uuid4())),
                },
            )
    engine.dispose()


def seed_dict_security_evidence(assessments: list[dict]) -> None:
    """Insere evidência DICT ``clear``, ``suspicious`` ou confirmada."""
    if not assessments:
        return
    engine = _engine()
    with engine.begin() as connection:
        for assessment in assessments:
            account_id = _account(connection, assessment["document"])
            connection.execute(
                text(
                    """
                    INSERT INTO external_risk_assessment
                        (account_id, axis, assessment, valid_from, valid_until,
                         provenance, occurrence_id, severity, source_version, observed_at)
                    VALUES (:account_id, :axis, :assessment, :valid_from, :valid_until,
                            :provenance, :occurrence_id, :severity, :source_version, :observed_at)
                    ON CONFLICT (occurrence_id) DO UPDATE SET
                        account_id = EXCLUDED.account_id,
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
                    "account_id": account_id,
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


seed_external_risk = seed_dict_security_evidence
