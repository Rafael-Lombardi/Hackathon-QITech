from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import joinedload

from database import Context
from models import PixAnalysis, PixTransaction, PixTransactionStatus


class PixTransactionRepository:
    """O Pix observado, sua análise e o catálogo de decisões.

    Não dá commit: create e create_signals deixam tudo na transação
    aberta, e o commit é a última linha do controller.
    """

    # Os ids das tabelas-enum, por enumerator. Vão pra memória na primeira
    # consulta e ficam lá, compartilhados por todas as requisições do
    # processo: eles não mudam depois do INSERT do database.sql.
    _status_ids: dict[str, int] = {}

    def __init__(self, context: Context) -> None:
        self.session = context.db_session

    def get_by_external_id(self, external_id: str) -> PixTransaction | None:
        """O Pix pelo id do payload — a idempotência do POST e o {id} do GET.

        O status vem no mesmo SELECT: quem chama sempre precisa do
        enumerator dele pra responder.
        """
        return self.session.scalars(
            select(PixTransaction)
            .options(joinedload(PixTransaction.analysis).joinedload(PixAnalysis.status, innerjoin=True))
            .where(PixTransaction.external_id == external_id)
        ).one_or_none()

    def create(self, **fields) -> PixTransaction | None:
        """Grava o Pix e devolve o model com o id preenchido, ou None se o external_id já estava gravado.

        fields usa os nomes das colunas do fato observado. A análise é
        inserida depois, na mesma transação SQL.

        None é a corrida do mesmo id chegando junto por pagadores
        diferentes, que a trava (uma por pagador) não serializa: o outro
        gravou primeiro, e este responde como num reenvio. O ON CONFLICT
        (external_id) DO NOTHING segura o nosso INSERT até o outro
        terminar e, se ele commitou, desiste sem erro. Capturar o
        IntegrityError daria no mesmo, a um preço: o Postgres grava no log
        dele todo comando que falha, com os valores — o payload e o CPF sem
        máscara —, e o controller teria de conhecer o nome da constraint.
        Outra violação, um CHECK por exemplo, continua levantando.
        """
        return self.session.scalars(
            insert(PixTransaction)
            .values(pix_transaction_key=str(uuid4()), **fields)
            .on_conflict_do_nothing(index_elements=[PixTransaction.external_id])
            .returning(PixTransaction)
        ).one_or_none()

    def create_analysis(self, **fields) -> PixAnalysis:
        """Grava o único resultado da avaliação, sem commit."""
        analysis = PixAnalysis(**fields)
        self.session.add(analysis)
        self.session.flush()
        return analysis

    def get_status_id(self, enumerator: str) -> int:
        """O id de um veredicto ("approved", "rejected", "two_factor_required")."""
        return self._enum_ids(PixTransactionStatus, PixTransactionRepository._status_ids)[enumerator]

    def _enum_ids(
        self,
        model: type[PixTransactionStatus],
        cache: dict[str, int],
    ) -> dict[str, int]:
        # Uma consulta traz a tabela inteira, que são três linhas. Repare
        # que o dict fica pronto ANTES de entrar no cache, num update só:
        # preenchido linha a linha, outra thread podia achar o cache pela
        # metade e levar um KeyError — sem a extensão C do SQLAlchemy foram
        # 400 leituras parciais em 884 mil. Enumerator que não existe
        # também vira KeyError, mas aí é erro de programação, e tem de
        # aparecer alto.
        if not cache:
            rows = self.session.execute(select(model.enumerator, model.id)).tuples().all()
            cache.update(dict(rows))
        return cache
