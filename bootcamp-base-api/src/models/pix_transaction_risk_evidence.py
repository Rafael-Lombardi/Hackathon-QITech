"""Fontes autoritativas observadas durante uma análise Pix."""

from sqlalchemy import BigInteger, CheckConstraint, Column, DateTime, ForeignKey, Index, String, UniqueConstraint, func

from models.base import Base
from models.pix_transaction import PixTransaction


class PixTransactionRiskEvidence(Base):
    """Snapshot imutável das fontes externas usadas para decidir um Pix.

    A tabela não representa o estado atual da fonte. Ela representa o que a
    análise consultou, permitindo reconstruir a decisão mesmo depois que uma
    fixture DICT ou lista de restrição for alterada.
    """

    __tablename__ = "pix_transaction_risk_evidence"

    id = Column(BigInteger, primary_key=True)
    pix_transaction_id = Column(BigInteger, ForeignKey(PixTransaction.id), nullable=False)
    source_type = Column(String(30), nullable=False)
    risk_subject = Column(String(10))
    scope = Column(String(30))
    assessment = Column(String(30))
    provenance = Column(String(80), nullable=False)
    occurrence_id = Column(String(255), nullable=False)
    valid_from = Column(DateTime(timezone=True), nullable=False)
    valid_until = Column(DateTime(timezone=True))
    captured_at = Column(DateTime(timezone=True), nullable=False)
    severity = Column(String(20))
    source_version = Column(String(50))
    observed_at = Column(DateTime(timezone=True))
    created_at = Column(DateTime, nullable=False, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("pix_transaction_id", "source_type", "occurrence_id"),
        Index("pix_transaction_risk_evidence_transaction_idx", "pix_transaction_id"),
        CheckConstraint("source_type IN ('restriction', 'dict_security')"),
        CheckConstraint("risk_subject IS NULL OR risk_subject IN ('payer', 'recipient')"),
        CheckConstraint("source_type <> 'restriction' OR scope IS NOT NULL"),
        CheckConstraint("source_type <> 'dict_security' OR risk_subject IS NOT NULL"),
        CheckConstraint("provenance <> ''"),
        CheckConstraint("occurrence_id <> ''"),
        CheckConstraint("valid_until IS NULL OR valid_until > valid_from"),
    )
