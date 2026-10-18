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
from models.customer import Customer


class Account(Base):
    """Uma conta de um customer, em qualquer banco.

    O titular faz parte da identidade: o UNIQUE é (participant, branch,
    account_number, customer_id), e uma conta conjunta vira uma linha por
    titular. O porquê disso, e de participant ficar None na conta de
    origem (daí o NULLS NOT DISTINCT), está no database.sql. Repare que o
    postgresql_nulls_not_distinct daqui só muda o DDL que o SQLAlchemy
    geraria; quem garante a regra é o UNIQUE do database.sql, e é nele que
    o ON CONFLICT do upsert se apoia.

    opening_date é a da PRIMEIRA aparição.
    """

    __tablename__ = "account"

    id = Column(BigInteger, primary_key=True)
    account_key = Column(CHAR(36), nullable=False)
    customer_id = Column(BigInteger, ForeignKey(Customer.id), nullable=False)
    participant = Column(CHAR(8))
    branch = Column(String(10), nullable=False)
    account_number = Column(String(20), nullable=False)
    opening_date = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(DateTime, nullable=False, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("account_key"),
        UniqueConstraint("participant", "branch", "account_number", "customer_id", postgresql_nulls_not_distinct=True),
        CheckConstraint("participant IS NULL OR participant ~ '^[0-9]{8}$'"),
        Index("account_customer_id_idx", "customer_id"),
    )
