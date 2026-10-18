from datetime import datetime
from uuid import uuid4

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert

from database import Context
from models import Account


class AccountRepository:
    """Identidades account-scoped e locks do caso de uso Pix."""

    def __init__(self, context: Context) -> None:
        self.session = context.db_session

    def get_or_create(
        self,
        *,
        document_number: str,
        participant: str | None,
        branch: str,
        account_number: str,
        opening_date: datetime,
    ) -> Account:
        identity = select(Account).where(
            Account.document_number == document_number,
            Account.branch == branch,
            Account.account_number == account_number,
        )
        if participant is None:
            identity = identity.where(Account.participant.is_(None))
        else:
            identity = identity.where(Account.participant == participant)

        account = self.session.scalars(identity).one_or_none()
        if account is not None:
            return account

        created = self.session.scalars(
            insert(Account)
            .values(
                account_key=str(uuid4()),
                document_number=document_number,
                participant=participant,
                branch=branch,
                account_number=account_number,
                opening_date=opening_date,
            )
            .on_conflict_do_nothing(
                index_elements=[
                    Account.participant,
                    Account.branch,
                    Account.account_number,
                    Account.document_number,
                ],
            )
            .returning(Account)
        ).one_or_none()
        if created is not None:
            return created
        return self.session.scalars(identity).one()

    def lock_for_pix_document(self, *, document_number: str) -> None:
        self.session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:document, 0))"),
            {"document": document_number},
        )

    def lock_for_pix_many(self, *, account_ids: tuple[int, ...]) -> None:
        ids = sorted(set(account_ids))
        if ids:
            self.session.execute(
                select(Account.id)
                .where(Account.id.in_(ids))
                .order_by(Account.id)
                .with_for_update()
            ).all()
