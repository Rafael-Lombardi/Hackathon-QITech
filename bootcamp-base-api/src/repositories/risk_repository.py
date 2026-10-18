"""Leitura das fontes autoritativas seedadas da política antifraude.

Os repositories devolvem snapshots simples. O controller pode consultar cada
fonte uma vez, congelar o resultado no ``EvaluationContext`` e garantir que
as camadas não façam consultas extras durante a avaliação.
"""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import case, or_, select

from database import Context
from models import ExternalRiskAssessment, RiskRestriction


@dataclass(frozen=True)
class RestrictionSnapshot:
    scope: str
    provenance: str
    occurrence_id: str
    valid_from: datetime
    valid_until: datetime | None


@dataclass(frozen=True)
class ExternalRiskAssessmentSnapshot:
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
    """Consulta restrições e inteligência externa vigentes no instante do Pix."""

    def __init__(self, context: Context) -> None:
        self.session = context.db_session

    def active_restriction(
        self,
        *,
        payer_customer_id: int,
        recipient_customer_id: int,
        recipient_account_id: int,
        at: datetime,
    ) -> RestrictionSnapshot | None:
        """Retorna a restrição mais específica encontrada.

        A ordenação torna o resultado determinístico quando uma pessoa e uma
        conta estão simultaneamente restritas: a conta exata vence o titular,
        que vence o pagador.
        """
        rows = self.session.scalars(
            select(RiskRestriction)
            .where(
                RiskRestriction.status == "active",
                RiskRestriction.active_from <= at,
                or_(RiskRestriction.active_until.is_(None), RiskRestriction.active_until > at),
                or_(
                    (RiskRestriction.scope == "payer") & (RiskRestriction.customer_id == payer_customer_id),
                    (RiskRestriction.scope == "recipient_holder")
                    & (RiskRestriction.customer_id == recipient_customer_id),
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

    def external_assessment(
        self,
        *,
        customer_id: int,
        axis: str,
        at: datetime,
    ) -> ExternalRiskAssessmentSnapshot | None:
        """Retorna o assessment vigente mais recente para o eixo informado."""
        row = self.session.scalar(
            select(ExternalRiskAssessment)
            .where(
                ExternalRiskAssessment.customer_id == customer_id,
                ExternalRiskAssessment.axis == axis,
                ExternalRiskAssessment.valid_from <= at,
                or_(
                    ExternalRiskAssessment.valid_until.is_(None),
                    ExternalRiskAssessment.valid_until > at,
                ),
            )
            .order_by(ExternalRiskAssessment.valid_from.desc(), ExternalRiskAssessment.id.desc())
            .limit(1)
        )
        if row is None:
            return None
        return ExternalRiskAssessmentSnapshot(
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
