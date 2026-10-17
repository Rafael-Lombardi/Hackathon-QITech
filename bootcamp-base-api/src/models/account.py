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
from sqlalchemy.orm import relationship

from models.base import Base
from models.customer import Customer


class Account(Base):
    """Uma conta de um customer, em qualquer banco — e a linha que trava o Pix.

    participant é o ISPB e fica None na conta de origem, que o payload
    traz sem ele. Daí o postgresql_nulls_not_distinct no UNIQUE da
    tripla: sem ele, None nunca seria igual a None e a conta de origem
    nasceria de novo a cada Pix. Repare que o argumento só muda o DDL que
    o SQLAlchemy geraria; quem garante a regra é o UNIQUE NULLS NOT
    DISTINCT do database.sql, e é nele que o ON CONFLICT do upsert se
    apoia.

    customer_id e opening_date são os da PRIMEIRA aparição. Conta que
    volta com outro documento ou outra data de abertura não é atualizada:
    o AccountRepository devolve a linha como está, e quem compara com o
    payload é o controller.
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
        UniqueConstraint("participant", "branch", "account_number", postgresql_nulls_not_distinct=True),
        CheckConstraint("participant IS NULL OR participant ~ '^[0-9]{8}$'"),
        Index("account_customer_id_idx", "customer_id"),
    )

    customer = relationship("Customer", foreign_keys=[customer_id])
