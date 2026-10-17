from uuid import uuid4

from sqlalchemy import insert, select
from sqlalchemy.orm import contains_eager, joinedload

from database import Context
from models import PixSignalStatus, PixTransaction, PixTransactionRuleResult, PixTransactionStatus


class PixTransactionRepository:
    """O Pix gravado, os sinais dele e os ids das duas tabelas-enum.

    Não dá commit: create e create_signals deixam tudo na transação
    aberta, e o commit é a última linha do controller.
    """

    # Os ids das tabelas-enum, por enumerator. Vão pra memória na primeira
    # consulta e ficam lá, compartilhados por todas as requisições do
    # processo: eles não mudam depois do INSERT do database.sql, e uma
    # base recriada do zero gera os mesmos.
    _status_ids: dict[str, int] = {}
    _signal_status_ids: dict[str, int] = {}

    def __init__(self, context: Context) -> None:
        self.session = context.get_or_create_session()

    def get_by_external_id(self, external_id: str) -> PixTransaction | None:
        """O Pix pelo id do payload — a idempotência do POST e o {id} do GET.

        O status vem no mesmo SELECT: quem chama sempre precisa do
        enumerator dele pra responder.
        """
        return self.session.scalars(
            select(PixTransaction)
            .options(joinedload(PixTransaction.status))
            .where(PixTransaction.external_id == external_id)
        ).one_or_none()

    def create(self, **fields) -> PixTransaction:
        """Grava o Pix e devolve o model com o id preenchido.

        fields usa os nomes das colunas (status_id, payer_risk,
        pix_transaction_data...); só a pix_transaction_key nasce aqui. O
        flush manda o INSERT agora, sem commit, por dois motivos: os sinais
        precisam do id, e a violação do UNIQUE(external_id) — dois POSTs do
        mesmo Pix chegando juntos — estoura nesta linha, onde o controller
        a espera, e não no commit.
        """
        pix_transaction = PixTransaction(pix_transaction_key=str(uuid4()), **fields)
        self.session.add(pix_transaction)
        self.session.flush()
        return pix_transaction

    def create_signals(self, pix_transaction_id: int, signals: list[dict]) -> None:
        """Grava os sinais de um Pix num INSERT só, uma linha por sinal.

        Cada dict usa os nomes das colunas (rule_name, signal_status_id,
        axis, family, points, rule_result_data); o pix_transaction_id
        entra aqui. Passar a lista inteira ao session.execute vira um
        INSERT ... VALUES (...), (...) — o insertmanyvalues do SQLAlchemy
        2.0. Um INSERT por sinal custaria o dobro: medido com 8 clientes,
        550 Pix por segundo em vez de 1.000.
        """
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
            .join(PixTransactionRuleResult.signal_status)
            .options(contains_eager(PixTransactionRuleResult.signal_status))
            .where(PixTransactionRuleResult.pix_transaction_id == pix_transaction_id)
            .order_by(PixTransactionRuleResult.id)
        ))

    def get_status_id(self, enumerator: str) -> int:
        """O id de um veredicto ("approved", "rejected", "two_factor_required")."""
        return self._enum_ids(PixTransactionStatus, PixTransactionRepository._status_ids)[enumerator]

    def get_signal_status_id(self, enumerator: str) -> int:
        """O id de um estado de sinal ("triggered", "not_triggered", "unavailable")."""
        return self._enum_ids(PixSignalStatus, PixTransactionRepository._signal_status_ids)[enumerator]

    def _enum_ids(self, model: type[PixTransactionStatus] | type[PixSignalStatus], cache: dict[str, int]) -> dict[str, int]:
        # Uma consulta traz a tabela inteira (são três linhas), e não só o
        # enumerator pedido: assim o cache enche de uma vez. Enumerator que
        # não existe vira KeyError — erro de programação, que tem de
        # aparecer alto.
        if not cache:
            cache.update(self.session.execute(select(model.enumerator, model.id)).tuples().all())
        return cache
