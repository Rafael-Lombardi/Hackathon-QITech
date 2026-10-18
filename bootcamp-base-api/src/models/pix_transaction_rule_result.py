from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    SmallInteger,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship

from models.base import Base
from models.pix_signal_status import PixSignalStatus
from models.pix_transaction import PixTransaction


class PixTransactionRuleResult(Base):
    """Um sinal avaliado num Pix: a linha que vira um item de metadata.signals.

    rule_name é o código da regra (P03, REL02...), e o estado vem por
    signal_status. A tabela é só de INSERT: o PixTransactionRepository
    grava os sinais de um Pix num INSERT só e os lê de volta na ordem em
    que gravou.
    """

    __tablename__ = "pix_transaction_rule_result"

    id = Column(BigInteger, primary_key=True)
    pix_transaction_id = Column(BigInteger, ForeignKey(PixTransaction.id), nullable=False)
    signal_status_id = Column(Integer, ForeignKey(PixSignalStatus.id), nullable=False)
    rule_result_data = Column(JSONB, nullable=False)
    rule_name = Column(String(50), nullable=False)
    risk_subject = Column(String(10), nullable=False)
    family = Column(String(30), nullable=False)
    points = Column(SmallInteger, nullable=False)
    created_at = Column(DateTime, nullable=False, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("pix_transaction_id", "rule_name"),
        CheckConstraint("rule_name <> ''"),
        CheckConstraint("risk_subject IN ('payer', 'recipient')"),
        CheckConstraint("family <> ''"),
        CheckConstraint("points BETWEEN -100 AND 100"),
        CheckConstraint("jsonb_typeof(rule_result_data) = 'object'"),
    )

    signal_status = relationship("PixSignalStatus", foreign_keys=[signal_status_id])
