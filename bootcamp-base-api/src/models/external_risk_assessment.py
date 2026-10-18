"""Avaliações de inteligência externa usadas pela política antifraude."""

from sqlalchemy import BigInteger, Column, DateTime, ForeignKey, Index, String, UniqueConstraint

from models.base import Base
from models.customer import Customer


class ExternalRiskAssessment(Base):
    """Snapshot seedado de reputação para um documento e eixo de risco."""

    __tablename__ = "external_risk_assessment"

    id = Column(BigInteger, primary_key=True)
    customer_id = Column(BigInteger, ForeignKey(Customer.id), nullable=False)
    axis = Column(String(10), nullable=False)
    assessment = Column(String(30), nullable=False)
    valid_from = Column(DateTime(timezone=True), nullable=False)
    valid_until = Column(DateTime(timezone=True))
    provenance = Column(String(80), nullable=False)
    occurrence_id = Column(String(255), nullable=False)

    __table_args__ = (
        UniqueConstraint("occurrence_id"),
        Index("external_risk_customer_axis_idx", "customer_id", "axis", "valid_from"),
    )
