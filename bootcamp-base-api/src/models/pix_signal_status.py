from sqlalchemy import Column, DateTime, Integer, String, UniqueConstraint, func

from models.base import Base


class PixSignalStatus(Base):
    """Os três estados de um sinal: disparou, não disparou, não tinha como responder.

    Mesmo arranjo do PixTransactionStatus: o código usa o enumerator, e o
    id vem do PixTransactionRepository. Por que unavailable não é a mesma
    coisa que not_triggered está no comentário da tabela, no
    database.sql.
    """

    __tablename__ = "pix_signal_status"

    id = Column(Integer, primary_key=True)
    enumerator = Column(String(50), nullable=False)
    created_at = Column(DateTime, nullable=False, server_default=func.now())

    __table_args__ = (UniqueConstraint("enumerator"),)

    TRIGGERED = "triggered"
    NOT_TRIGGERED = "not_triggered"
    UNAVAILABLE = "unavailable"
