from datetime import datetime
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from database import Context
from models import Account


class AccountRepository:
    """As contas, de origem e de destino, e a trava do Pix."""

    def __init__(self, context: Context) -> None:
        self.session = context.get_or_create_session()

    def get_or_create(
        self,
        customer_id: int,
        participant: str | None,
        branch: str,
        account_number: str,
        opening_date: datetime,
    ) -> Account:
        """Devolve a conta da tripla (participant, branch, account_number), criando na primeira aparição.

        É o mesmo upsert do CustomerRepository.get_or_create, com o porquê
        escrito lá. Duas diferenças.

        A busca precisa de um if. participant é None na conta de origem, e
        `participant = NULL` nunca é verdade em SQL: a conta não seria
        achada nunca, e todo Pix cairia no INSERT. IS NOT DISTINCT FROM
        resolveria numa linha só, mas o Postgres não usa índice com ele —
        cada Pix leria a tabela de contas inteira. IS NULL usa o índice do
        UNIQUE. O ON CONFLICT dispensa o if: o UNIQUE é NULLS NOT DISTINCT,
        e para ele None é igual a None.

        E a linha volta como foi gravada na primeira vez. Se o payload
        trouxer outro customer ou outra opening_date para a mesma conta,
        nada é atualizado: quem compara e decide é o controller.
        """
        by_identity = select(Account).where(Account.branch == branch, Account.account_number == account_number)
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
            .on_conflict_do_nothing(index_elements=[Account.participant, Account.branch, Account.account_number])
            .returning(Account)
        ).one_or_none()
        if created is not None:
            return created

        return self.session.scalars(by_identity).one()

    def lock_for_pix(self, account_id: int) -> None:
        """Trava a conta de origem até o fim da transação: SELECT ... FOR NO KEY UPDATE.

        Dois Pix da mesma conta chegando juntos passam um de cada vez: o
        segundo espera aqui o commit (ou o rollback) do primeiro e, quando
        segue, já enxerga o Pix que o primeiro gravou. É o que faz as
        contagens de rajada verem a tentativa anterior.

        Repare no NO KEY. O INSERT de um Pix confere as duas FKs com um
        FOR KEY SHARE nas contas, e um FOR UPDATE comum brigaria com ele:
        Pix recebido por esta conta esperaria os enviados por ela, e dois
        Pix cruzados (1 paga 4, 4 paga 1) dariam deadlock. Com NO KEY
        UPDATE os dois convivem.

        Só funciona em READ COMMITTED, o padrão. Em REPEATABLE READ a
        segunda transação espera e mesmo assim lê o histórico de antes da
        espera.
        """
        self.session.execute(
            select(Account.id).where(Account.id == account_id).with_for_update(key_share=True)
        ).scalar_one()
