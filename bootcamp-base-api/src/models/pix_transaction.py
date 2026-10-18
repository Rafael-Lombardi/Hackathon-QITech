from sqlalchemy import (
    CHAR,
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship

from models.account import Account
from models.base import Base


class PixTransaction(Base):
    """Um Pix recebido: o fato observado e o payload original.

    O porquê de cada coluna está no comentário da tabela, no
    database.sql. Aqui ficam dois detalhes do mapeamento que alguém vai
    errar.

    transaction_date volta do banco com fuso, só que no fuso da sessão
    (UTC no container), e não no -03:00 que chegou. É o mesmo instante. A
    hora de São Paulo sai de astimezone — ler .hour direto erra por três
    horas.

    latitude e longitude são Numeric(9, 6) e voltam Decimal, que o
    json.dumps não serializa sozinho.
    """

    __tablename__ = "pix_transaction"

    id = Column(BigInteger, primary_key=True)
    pix_transaction_key = Column(CHAR(36), nullable=False)
    source_account_id = Column(BigInteger, ForeignKey(Account.id), nullable=False)
    destination_account_id = Column(BigInteger, ForeignKey(Account.id), nullable=False)
    pix_transaction_data = Column(JSONB, nullable=False)
    external_id = Column(String(255), nullable=False)
    amount = Column(BigInteger, nullable=False)
    transaction_date = Column(DateTime(timezone=True), nullable=False)
    device_id = Column(String(255), nullable=False)
    channel = Column(String(30), nullable=False)
    ip = Column(String(45), nullable=False)
    latitude = Column(Numeric(9, 6))
    longitude = Column(Numeric(9, 6))
    created_at = Column(DateTime, nullable=False, server_default=func.now())
    received_at = Column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint("pix_transaction_key"),
        UniqueConstraint("external_id"),
        CheckConstraint("jsonb_typeof(pix_transaction_data) = 'object'"),
        CheckConstraint("amount > 0"),
        CheckConstraint("(latitude IS NULL) = (longitude IS NULL)"),
        CheckConstraint("latitude IS NULL OR latitude BETWEEN -90 AND 90"),
        CheckConstraint("longitude IS NULL OR longitude BETWEEN -180 AND 180"),
        Index("pix_transaction_source_account_id_idx", "source_account_id", "received_at"),
        Index("pix_transaction_destination_account_id_idx", "destination_account_id", "received_at"),
        Index("pix_transaction_device_id_idx", "device_id", "received_at"),
    )

    analysis = relationship("PixAnalysis", back_populates="pix_transaction", uselist=False)
