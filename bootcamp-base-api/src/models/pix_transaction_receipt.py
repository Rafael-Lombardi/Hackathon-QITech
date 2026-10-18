from sqlalchemy import BigInteger, Column, DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB

from models.base import Base


class PixTransactionReceipt(Base):
    """Durable intake/claim record, independent of the final verdict."""

    __tablename__ = "pix_transaction_receipt"

    id = Column(BigInteger, primary_key=True)
    external_id = Column(String(255), nullable=False, unique=True)
    payload = Column(JSONB, nullable=False)
    state = Column(String(20), nullable=False, default="processing")
    owner_token = Column(String(36), nullable=False)
    lease_until = Column(DateTime(timezone=True), nullable=False)
    received_at = Column(DateTime(timezone=True), nullable=False)
    evaluated_at = Column(DateTime(timezone=True))
    failure_reason = Column(Text)
    pix_transaction_id = Column(BigInteger, ForeignKey("pix_transaction.id"))
    created_at = Column(DateTime, nullable=False, server_default=func.now())
