from sqlalchemy import (
    CHAR,
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    func,
)

from models.base import Base
class Account(Base):
    """Identidade e conta bancária do pagador/recebedor.

    O domínio deliberadamente é account-scoped: uma linha identifica o
    documento e a conta usada no Pix; não existe uma entidade de titular
    separada no fluxo principal.
    """

    __tablename__ = "account"

    id = Column(BigInteger, primary_key=True)
    account_key = Column(CHAR(36), nullable=False)
    document_number = Column(String(14), nullable=False)
    participant = Column(CHAR(8))
    branch = Column(String(10), nullable=False)
    account_number = Column(String(20), nullable=False)
    opening_date = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(DateTime, nullable=False, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("account_key"),
        UniqueConstraint("participant", "branch", "account_number", "document_number", postgresql_nulls_not_distinct=True),
        CheckConstraint("participant IS NULL OR participant ~ '^[0-9]{8}$'"),
        CheckConstraint("document_number ~ '^[0-9]{11}$' OR document_number ~ '^[0-9]{14}$'"),
        Index("account_document_number_idx", "document_number"),
    )
