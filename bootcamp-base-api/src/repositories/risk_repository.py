"""Leitura das fontes autoritativas seedadas da política antifraude.

Os repositories devolvem snapshots simples. O controller pode consultar cada
fonte uma vez, congelar o resultado no ``EvaluationContext`` e garantir que
as camadas não façam consultas extras durante a avaliação.
"""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import case, or_, select

from database import Context
from models import Account, DictSecurityEvidence, RiskRestriction


@dataclass(frozen=True)
class RestrictionSnapshot:
    scope: str
    provenance: str
    occurrence_id: str
    valid_from: datetime
    valid_until: datetime | None


@dataclass(frozen=True)
class DictSecurityEvidenceSnapshot:
    axis: str
    assessment: str
    provenance: str
    occurrence_id: str
    valid_from: datetime
    valid_until: datetime | None
    severity: str | None = None
    source_version: str | None = None
    observed_at: datetime | None = None


class RiskRepository:
    """Consulta restrições e evidência antifraude do DICT."""

    def __init__(self, context: Context) -> None:
        self.session = context.db_session

    def active_restriction(
        self,
        *,
        payer_account_id: int,
        recipient_account_id: int,
        at: datetime,
    ) -> RestrictionSnapshot | None:
        """Retorna a restrição mais específica encontrada.

        A ordenação torna o resultado determinístico quando uma pessoa e uma
        conta estão simultaneamente restritas: a conta exata vence o titular,
        que vence o pagador.
        """
        payer_document = select(Account.document_number).where(Account.id == payer_account_id).scalar_subquery()
        recipient_document = select(Account.document_number).where(Account.id == recipient_account_id).scalar_subquery()
        rows = self.session.scalars(
            select(RiskRestriction)
            .join(Account, Account.id == RiskRestriction.account_id)
            .where(
                RiskRestriction.status == "active",
                RiskRestriction.active_from <= at,
                or_(RiskRestriction.active_until.is_(None), RiskRestriction.active_until > at),
                or_(
                    (RiskRestriction.scope == "payer") & (Account.document_number == payer_document),
                    (RiskRestriction.scope == "recipient_holder")
                    & (Account.document_number == recipient_document),
                    (RiskRestriction.scope == "recipient_account")
                    & (RiskRestriction.account_id == recipient_account_id),
                ),
            )
            .order_by(
                case(
                    (RiskRestriction.scope == "recipient_account", 0),
                    (RiskRestriction.scope == "recipient_holder", 1),
                    else_=2,
                ),
                RiskRestriction.id.asc(),
            )
        ).all()
        if not rows:
            return None
        row = rows[0]
        return RestrictionSnapshot(row.scope, row.provenance, row.occurrence_id, row.active_from, row.active_until)

    def dict_security_evidence(
        self,
        *,
        account_id: int,
        axis: str,
        at: datetime,
    ) -> DictSecurityEvidenceSnapshot | None:
        """Retorna evidência de segurança DICT vigente para o eixo."""
        document = select(Account.document_number).where(Account.id == account_id).scalar_subquery()
        row = self.session.scalar(
            select(DictSecurityEvidence)
            .join(Account, Account.id == DictSecurityEvidence.account_id)
            .where(
                Account.document_number == document,
                DictSecurityEvidence.axis == axis,
                DictSecurityEvidence.valid_from <= at,
                or_(
                    DictSecurityEvidence.valid_until.is_(None),
                    DictSecurityEvidence.valid_until > at,
                ),
            )
            .order_by(DictSecurityEvidence.valid_from.desc(), DictSecurityEvidence.id.desc())
            .limit(1)
        )
        if row is None:
            return None
        return DictSecurityEvidenceSnapshot(
            axis=row.axis,
            assessment=row.assessment,
            provenance=row.provenance,
            occurrence_id=row.occurrence_id,
            valid_from=row.valid_from,
            valid_until=row.valid_until,
            severity=row.severity,
            source_version=row.source_version,
            observed_at=row.observed_at,
        )
