from datetime import datetime

from sqlalchemy import and_, exists, func, or_, select

from database import Context
from models import Account, DeviceRegistry, PixTransaction
from antifraud.contract import LOCAL_TIMEZONE


class DeviceRegistryRepository:
    """Leitura do cadastro autoritativo de aparelhos do pagador."""

    def __init__(self, context: Context) -> None:
        self.session = context.db_session

    def is_trusted(self, *, customer_id: int, device_id: str, at: datetime) -> bool:
        """Retorna confiança somente durante o período cadastrado."""
        row = self.session.scalar(
            select(DeviceRegistry.id)
            .where(
                DeviceRegistry.customer_id == customer_id,
                DeviceRegistry.device_id == device_id,
                DeviceRegistry.registered_at <= at,
                or_(DeviceRegistry.revoked_at.is_(None), DeviceRegistry.revoked_at > at),
            )
            .limit(1)
        )
        return row is not None

    def unregistered_device_day_amount(self, *, customer_id: int, at: datetime) -> int:
        """Soma hoje os Pix de aparelhos sem cadastro no instante observado."""
        local = at.astimezone(LOCAL_TIMEZONE)
        day_start = local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(at.tzinfo)
        registered_at_transaction = exists(
            select(DeviceRegistry.id).where(
                DeviceRegistry.customer_id == customer_id,
                DeviceRegistry.device_id == PixTransaction.device_id,
                DeviceRegistry.registered_at <= PixTransaction.transaction_date,
                or_(DeviceRegistry.revoked_at.is_(None), DeviceRegistry.revoked_at > PixTransaction.transaction_date),
            )
        )
        amount = self.session.scalar(
            select(func.coalesce(func.sum(PixTransaction.amount), 0))
            .join(Account, Account.id == PixTransaction.source_account_id)
            .where(
                Account.customer_id == customer_id,
                PixTransaction.transaction_date >= day_start,
                PixTransaction.transaction_date <= at,
                ~registered_at_transaction,
            )
        )
        return int(amount or 0)
