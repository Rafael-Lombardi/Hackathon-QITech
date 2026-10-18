from sqlalchemy import CHAR, BigInteger, CheckConstraint, Column, DateTime, String, UniqueConstraint, func

from models.base import Base


class Customer(Base):
    """Quem paga ou recebe: um por documento, só dígitos (11 ou 14).

    Nasce na primeira vez que o documento aparece num payload, pelo
    CustomerRepository.get_or_create, e não muda depois. As regras por
    pessoa do motor agrupam aqui, somando todas as contas do documento, e
    é por isso que o customer do pagador é a linha que o controller trava
    (CustomerRepository.lock_for_pix; o porquê está no database.sql).
    """

    __tablename__ = "customer"

    id = Column(BigInteger, primary_key=True)
    customer_key = Column(CHAR(36), nullable=False)
    document_number = Column(String(14), nullable=False)
    created_at = Column(DateTime, nullable=False, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("customer_key"),
        UniqueConstraint("document_number"),
        CheckConstraint("document_number ~ '^[0-9]{11}$' OR document_number ~ '^[0-9]{14}$'"),
    )
