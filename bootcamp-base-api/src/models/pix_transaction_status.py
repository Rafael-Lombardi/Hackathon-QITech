from sqlalchemy import Column, DateTime, Integer, String, UniqueConstraint, func

from models.base import Base


class PixTransactionStatus(Base):
    """Os três veredictos do contrato — as linhas que o database.sql insere.

    O código fala em enumerator ("approved"), nunca em id. O id sai do
    SERIAL na ordem do INSERT, e escrever 1, 2 e 3 no código amarraria a
    regra a um detalhe que o SQL não promete. Quem precisa do id pede ao
    PixTransactionRepository, que guarda os três na primeira consulta.
    """

    __tablename__ = "pix_transaction_status"

    id = Column(Integer, primary_key=True)
    enumerator = Column(String(50), nullable=False)
    created_at = Column(DateTime, nullable=False, server_default=func.now())

    __table_args__ = (UniqueConstraint("enumerator"),)

    APPROVED = "approved"
    REJECTED = "rejected"
    TWO_FACTOR_REQUIRED = "two_factor_required"
