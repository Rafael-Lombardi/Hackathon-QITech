"""Restrições autoritativas aplicáveis a uma avaliação Pix."""

from sqlalchemy import BigInteger, Column, DateTime, ForeignKey, Index, String, UniqueConstraint, func

from models.base import Base
from models.account import Account


class RiskRestriction(Base):
    """Seed/mock da lista de restrições do motor.

    A restrição é uma fonte externa, não um evento observado. ``scope``
    diferencia bloqueio do pagador, do titular do destino e da conta exata
    do destino. A validade temporal permite testar expiração sem apagar
    evidência histórica.
    """

    __tablename__ = "risk_restriction"

    id = Column(BigInteger, primary_key=True)
    scope = Column(String(30), nullable=False)
    account_id = Column(BigInteger, ForeignKey(Account.id), nullable=False)
    active_from = Column(DateTime(timezone=True), nullable=False)
    active_until = Column(DateTime(timezone=True))
    status = Column(String(20), nullable=False, default="active")
    provenance = Column(String(80), nullable=False)
    occurrence_id = Column(String(255), nullable=False)
    created_at = Column(DateTime, nullable=False, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("occurrence_id"),
        Index("risk_restriction_account_idx", "account_id", "active_from"),
    )
