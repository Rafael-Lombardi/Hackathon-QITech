from sqlalchemy import BigInteger, Column, DateTime, ForeignKey, Index, String, UniqueConstraint

from models.base import Base
from models.customer import Customer


class DeviceRegistry(Base):
    """Cadastro independente de aparelhos confiáveis de um pagador.

    Um Pix aprovado apenas gera observação no histórico; ele nunca cria esta
    linha. O cadastro é alimentado por seed/serviço de identidade externo.
    """

    __tablename__ = "device_registry"

    id = Column(BigInteger, primary_key=True)
    customer_id = Column(BigInteger, ForeignKey(Customer.id), nullable=False)
    device_id = Column(String(255), nullable=False)
    registered_at = Column(DateTime(timezone=True), nullable=False)
    revoked_at = Column(DateTime(timezone=True))
    provenance = Column(String(50), nullable=False)

    __table_args__ = (
        UniqueConstraint("customer_id", "device_id"),
        Index("device_registry_customer_device_idx", "customer_id", "device_id"),
    )
