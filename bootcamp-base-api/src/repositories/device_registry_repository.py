from datetime import datetime

from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.orm import aliased

from database import Context
from models import Account, DeviceRegistry, PixTransaction
from antifraud.contract import LOCAL_TIMEZONE


class DeviceRegistryRepository:
    """Leitura do cadastro autoritativo de aparelhos do pagador."""

    def __init__(self, context: Context) -> None:
        self.session = context.db_session

    def is_trusted(self, *, account_id: int, device_id: str, at: datetime) -> bool:
        """Retorna confiança somente durante o período cadastrado."""
        document = select(Account.document_number).where(Account.id == account_id).scalar_subquery()
        row = self.session.scalar(
            select(DeviceRegistry.id)
            .join(Account, Account.id == DeviceRegistry.account_id)
            .where(
                Account.document_number == document,
                DeviceRegistry.device_id == device_id,
                DeviceRegistry.registered_at <= at,
                or_(DeviceRegistry.revoked_at.is_(None), DeviceRegistry.revoked_at > at),
            )
            .limit(1)
        )
        return row is not None

    def unregistered_device_day_amount(self, *, account_id: int, at: datetime) -> int:
        """Soma hoje os Pix de aparelhos sem cadastro no instante observado."""
        local = at.astimezone(LOCAL_TIMEZONE)
        day_start = local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(at.tzinfo)
        registry_account = aliased(Account)
        registered_at_transaction = exists(
            select(DeviceRegistry.id)
            .join(registry_account, registry_account.id == DeviceRegistry.account_id)
            .where(
                registry_account.document_number == Account.document_number,
                DeviceRegistry.device_id == PixTransaction.device_id,
                DeviceRegistry.registered_at <= PixTransaction.received_at,
                or_(DeviceRegistry.revoked_at.is_(None), DeviceRegistry.revoked_at > PixTransaction.received_at),
            )
        )
        amount = self.session.scalar(
            select(func.coalesce(func.sum(PixTransaction.amount), 0))
            .join(Account, Account.id == PixTransaction.source_account_id)
            .where(
                Account.id == account_id,
                PixTransaction.received_at >= day_start,
                PixTransaction.received_at < at,
                ~registered_at_transaction,
            )
        )
        return int(amount or 0)
