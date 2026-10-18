from sqlalchemy import BigInteger, CheckConstraint, Column, DateTime, ForeignKey, Integer, SmallInteger, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship

from models.base import Base
from models.pix_transaction_status import PixTransactionStatus


class PixAnalysis(Base):
    """A decisão produzida pelo motor para um Pix observado.

    A transação é o fato recebido; esta linha é o resultado auditável.
    Existe no máximo uma análise por Pix neste fluxo síncrono.
    """

    __tablename__ = "pix_analysis"

    id = Column(BigInteger, primary_key=True)
    pix_transaction_id = Column(BigInteger, ForeignKey("pix_transaction.id"), nullable=False)
    status_id = Column(Integer, ForeignKey(PixTransactionStatus.id), nullable=False)
    payer_risk = Column(SmallInteger, nullable=False)
    recipient_risk = Column(SmallInteger, nullable=False)
    payer_sufficiency = Column(String(12), nullable=False)
    recipient_sufficiency = Column(String(12), nullable=False)
    policy_version = Column(String(20), nullable=False)
    decision_rule = Column(String(20), nullable=False)
    reasons = Column(JSONB, nullable=False)
    evaluated_at = Column(DateTime(timezone=True), nullable=False)
    analysis_metadata = Column("metadata", JSONB, nullable=False, default=dict)
    created_at = Column(DateTime, nullable=False, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("pix_transaction_id"),
        CheckConstraint("payer_risk BETWEEN 0 AND 100"),
        CheckConstraint("recipient_risk BETWEEN 0 AND 100"),
        CheckConstraint("payer_sufficiency IN ('unavailable', 'low', 'medium', 'high')"),
        CheckConstraint("recipient_sufficiency IN ('unavailable', 'low', 'medium', 'high')"),
        CheckConstraint("policy_version <> ''"),
        CheckConstraint("decision_rule <> ''"),
        CheckConstraint("jsonb_typeof(reasons) = 'array'"),
        CheckConstraint("jsonb_typeof(metadata) = 'object'"),
    )

    status = relationship("PixTransactionStatus", foreign_keys=[status_id])
    pix_transaction = relationship("PixTransaction", back_populates="analysis")
