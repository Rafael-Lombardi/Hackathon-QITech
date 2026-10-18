from enum import StrEnum

from sqlalchemy import CHAR, BigInteger, CheckConstraint, Column, DateTime, ForeignKey, String, Text, UniqueConstraint, func

from models.base import Base
from models.pix_transaction import PixTransaction


class ReceiptState(StrEnum):
    """Os três estados de um recibo, com o texto do CHECK da tabela.

    processing é um avaliador trabalhando (ou que morreu trabalhando: o
    lease diz qual); completed é análise gravada, e só esse estado tem
    pix_transaction_id; failed é avaliação que não chegou ao fim, com o
    motivo, à espera de um reenvio.
    """

    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class PixTransactionReceipt(Base):
    """O recibo de um id: chegou, está sendo avaliado, foi avaliado ou falhou.

    Nasce antes da avaliação, numa transação curta só dele, e por isso
    sobrevive a qualquer falha da avaliação. O porquê de cada coluna está
    no database.sql; o ciclo (reivindicação, lease, fencing pelo
    owner_token) está no PixTransactionRepository e no controller.
    """

    __tablename__ = "pix_transaction_receipt"

    id = Column(BigInteger, primary_key=True)
    external_id = Column(String(255), nullable=False)
    payload_hash = Column(CHAR(64), nullable=False)
    received_at = Column(DateTime(timezone=True), nullable=False)
    state = Column(String(10), nullable=False)
    owner_token = Column(CHAR(36), nullable=False)
    lease_until = Column(DateTime(timezone=True), nullable=False)
    evaluated_at = Column(DateTime(timezone=True))
    failure_reason = Column(Text)
    pix_transaction_id = Column(BigInteger, ForeignKey(PixTransaction.id))
    created_at = Column(DateTime, nullable=False, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("external_id"),
        CheckConstraint("state IN ('processing', 'completed', 'failed')"),
        CheckConstraint("payload_hash ~ '^[0-9a-f]{64}$'"),
        CheckConstraint("(state = 'completed') = (pix_transaction_id IS NOT NULL)"),
        CheckConstraint("state = 'failed' OR failure_reason IS NULL"),
    )
