from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import joinedload

from database import Context
from models import PixSignalStatus, PixTransaction, PixTransactionRuleResult, PixTransactionStatus


class PixTransactionRepository:
    """O Pix gravado, os sinais dele e os ids das duas tabelas-enum.

    Não dá commit: create e create_signals deixam tudo na transação
    aberta, e o commit é a última linha do controller.
    """

    # Os ids das tabelas-enum, por enumerator. Vão pra memória na primeira
    # consulta e ficam lá, compartilhados por todas as requisições do
    # processo: eles não mudam depois do INSERT do database.sql.
    _status_ids: dict[str, int] = {}
    _signal_status_ids: dict[str, int] = {}

    def __init__(self, context: Context) -> None:
        self.session = context.db_session

    def get_by_external_id(self, external_id: str) -> PixTransaction | None:
        """O Pix pelo id do payload — a idempotência do POST e o {id} do GET.

        O status vem no mesmo SELECT: quem chama sempre precisa do
        enumerator dele pra responder.
        """
        return self.session.scalars(
            select(PixTransaction)
            .options(joinedload(PixTransaction.status, innerjoin=True))
            .where(PixTransaction.external_id == external_id)
        ).one_or_none()

    def create(self, **fields) -> PixTransaction | None:
        """Grava o Pix e devolve o model com o id preenchido, ou None se o external_id já estava gravado.

        fields usa os nomes das colunas (status_id, payer_risk,
        pix_transaction_data...); só a pix_transaction_key nasce aqui. O
        INSERT sai agora, sem commit: os sinais precisam do id.

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

    def create_signals(self, pix_transaction_id: int, signals: list[dict]) -> None:
        """Grava os sinais de um Pix num INSERT só, uma linha por sinal.

        Cada dict usa os nomes das colunas (rule_name, signal_status_id,
        axis, family, points, rule_result_data); o pix_transaction_id
        entra aqui. Passar a lista inteira ao session.execute vira um
        INSERT ... VALUES (...), (...) — o insertmanyvalues do SQLAlchemy
        2.0. Um INSERT por sinal custaria o dobro (medido).
        """
        # Com a lista vazia, o SQLAlchemy mandaria um INSERT sem valores.
        if not signals:
            return

        rows = [{**signal, "pix_transaction_id": pix_transaction_id} for signal in signals]
        self.session.execute(insert(PixTransactionRuleResult), rows)

    def list_signals(self, pix_transaction_id: int) -> list[PixTransactionRuleResult]:
        """Os sinais de um Pix na ordem em que foram gravados, com o estado de cada um.

        O estado (signal_status.enumerator) vem no mesmo SELECT, por JOIN.
        A ordem é a do id: o INSERT multi-linha numera as linhas na ordem
        da lista que recebeu.
        """
        return list(self.session.scalars(
            select(PixTransactionRuleResult)
            .options(joinedload(PixTransactionRuleResult.signal_status, innerjoin=True))
            .where(PixTransactionRuleResult.pix_transaction_id == pix_transaction_id)
            .order_by(PixTransactionRuleResult.id)
        ))

    def get_status_id(self, enumerator: str) -> int:
        """O id de um veredicto ("approved", "rejected", "two_factor_required")."""
        return self._enum_ids(PixTransactionStatus, PixTransactionRepository._status_ids)[enumerator]

    def get_signal_status_id(self, enumerator: str) -> int:
        """O id de um estado de sinal ("triggered", "not_triggered", "unavailable")."""
        return self._enum_ids(PixSignalStatus, PixTransactionRepository._signal_status_ids)[enumerator]

    def _enum_ids(
        self,
        model: type[PixTransactionStatus] | type[PixSignalStatus],
        cache: dict[str, int],
    ) -> dict[str, int]:
        # Uma consulta traz a tabela inteira, que são três linhas. Repare
        # que o dict fica pronto ANTES de entrar no cache, num update só:
        # preenchido linha a linha, outra thread podia achar o cache pela
        # metade e levar um KeyError (aconteceu). Enumerator que não existe
        # também vira KeyError, mas aí é erro de programação, e tem de
        # aparecer alto.
        if not cache:
            rows = self.session.execute(select(model.enumerator, model.id)).tuples().all()
            cache.update(dict(rows))
        return cache
