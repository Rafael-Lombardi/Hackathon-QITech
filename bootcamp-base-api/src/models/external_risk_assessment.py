"""Evidência antifraude simulada retornada pelo DICT."""

from sqlalchemy import BigInteger, Column, DateTime, ForeignKey, Index, String, UniqueConstraint

from models.base import Base
from models.account import Account


class DictSecurityEvidence(Base):
    """Snapshot de segurança do DICT para um documento e eixo de risco."""

    __tablename__ = "external_risk_assessment"

    id = Column(BigInteger, primary_key=True)
    account_id = Column(BigInteger, ForeignKey(Account.id), nullable=False)
    axis = Column(String(10), nullable=False)
    assessment = Column(String(30), nullable=False)
    valid_from = Column(DateTime(timezone=True), nullable=False)
    valid_until = Column(DateTime(timezone=True))
    provenance = Column(String(80), nullable=False)
    occurrence_id = Column(String(255), nullable=False)
    severity = Column(String(20))
    source_version = Column(String(50))
    observed_at = Column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("occurrence_id"),
        Index("external_risk_account_axis_idx", "account_id", "axis", "valid_from"),
    )


# Compatibilidade interna durante transição do nome antigo da tabela/model.
ExternalRiskAssessment = DictSecurityEvidence
