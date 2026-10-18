from sqlalchemy import (
    CHAR,
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship

from models.account import Account
from models.base import Base
from models.pix_transaction_status import PixTransactionStatus


class PixTransaction(Base):
    """Um Pix recebido: o payload, as colunas que o motor leu e a decisão.

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
    status_id = Column(Integer, ForeignKey(PixTransactionStatus.id), nullable=False)
    payer_risk = Column(SmallInteger, nullable=False)
    recipient_risk = Column(SmallInteger, nullable=False)
    payer_sufficiency = Column(String(12), nullable=False)
    recipient_sufficiency = Column(String(12), nullable=False)
    policy_version = Column(String(20), nullable=False)
    decision_rule = Column(String(20), nullable=False)
    reasons = Column(JSONB, nullable=False)
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

    __table_args__ = (
        UniqueConstraint("pix_transaction_key"),
        UniqueConstraint("external_id"),
        CheckConstraint("jsonb_typeof(pix_transaction_data) = 'object'"),
        CheckConstraint("payer_risk BETWEEN 0 AND 100"),
        CheckConstraint("recipient_risk BETWEEN 0 AND 100"),
        CheckConstraint("payer_sufficiency IN ('unavailable', 'low', 'medium', 'high')"),
        CheckConstraint("recipient_sufficiency IN ('unavailable', 'low', 'medium', 'high')"),
        CheckConstraint("policy_version <> ''"),
        CheckConstraint("decision_rule <> ''"),
        CheckConstraint("jsonb_typeof(reasons) = 'array'"),
        CheckConstraint("amount > 0"),
        CheckConstraint("(latitude IS NULL) = (longitude IS NULL)"),
        CheckConstraint("latitude IS NULL OR latitude BETWEEN -90 AND 90"),
        CheckConstraint("longitude IS NULL OR longitude BETWEEN -180 AND 180"),
        Index("pix_transaction_source_account_id_idx", "source_account_id", "transaction_date"),
        Index("pix_transaction_destination_account_id_idx", "destination_account_id", "transaction_date"),
        Index("pix_transaction_device_id_idx", "device_id", "transaction_date"),
    )

    status = relationship("PixTransactionStatus", foreign_keys=[status_id])
