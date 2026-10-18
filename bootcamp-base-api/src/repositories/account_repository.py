from datetime import datetime
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from database import Context
from models import Account


class AccountRepository:
    """As contas, de origem e de destino."""

    def __init__(self, context: Context) -> None:
        self.session = context.db_session

    def get_or_create(
        self,
        *,
        customer_id: int,
        participant: str | None,
        branch: str,
        account_number: str,
        opening_date: datetime,
    ) -> Account:
        """Devolve a conta deste titular (participant, branch, account_number, customer_id), criando na primeira aparição.

        É o mesmo upsert do CustomerRepository.get_or_create, com o porquê
        escrito lá. Três diferenças.

        O titular entra na identidade. A mesma conta com outro documento —
        uma conta conjunta — é outra linha, e o Pix de cada titular fica no
        histórico do documento dele; o porquê está no database.sql.

        A busca precisa de um if. participant é None na conta de origem, e
        `participant = NULL` nunca é verdade em SQL: a conta não seria
        achada nunca, e todo Pix cairia no INSERT. IS NOT DISTINCT FROM
        resolveria as duas contas numa linha só, mas com um ISPB do lado
        de lá o Postgres não usa índice — medido com 44 mil contas, a conta
        de destino passa a ler a tabela inteira, 18 ms em vez de 0,1. Daí o
        if: IS NULL na origem, = no destino, e as duas buscas entram pelo
        índice do UNIQUE. O ON CONFLICT dispensa o if: o UNIQUE é NULLS NOT
        DISTINCT, e para ele None é igual a None.

        E opening_date não entra na identidade: a linha volta como foi
        gravada na primeira vez, sem UPDATE.
        """
        by_identity = select(Account).where(
            Account.branch == branch,
            Account.account_number == account_number,
            Account.customer_id == customer_id,
        )
        if participant is None:
            by_identity = by_identity.where(Account.participant.is_(None))
        else:
            by_identity = by_identity.where(Account.participant == participant)

        account = self.session.scalars(by_identity).one_or_none()
        if account is not None:
            return account

        created = self.session.scalars(
            insert(Account)
            .values(
                account_key=str(uuid4()),
                customer_id=customer_id,
                participant=participant,
                branch=branch,
                account_number=account_number,
                opening_date=opening_date,
            )
            .on_conflict_do_nothing(
                index_elements=[Account.participant, Account.branch, Account.account_number, Account.customer_id],
            )
            .returning(Account)
        ).one_or_none()
        if created is not None:
            return created

        return self.session.scalars(by_identity).one()
