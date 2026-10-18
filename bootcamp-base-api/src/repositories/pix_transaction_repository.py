from datetime import datetime
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import joinedload

from database import Context
from models import (
    PixSignalStatus,
    PixTransaction,
    PixTransactionReceipt,
    PixTransactionRuleResult,
    PixTransactionStatus,
    ReceiptState,
)


class PixTransactionRepository:
    """O Pix gravado, os sinais dele, o recibo de cada id e os ids das duas tabelas-enum.

    Não dá commit: cada método deixa o que fez na transação aberta, e
    quem commita é o controller — o recibo numa transação curta só dele,
    a análise junto com a conclusão do recibo.
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

    def get_receipt(self, external_id: str, *, for_update: bool = False) -> PixTransactionReceipt | None:
        """O recibo do id, se houver. for_update trava a linha até o fim da transação.

        A trava é pra reivindicação: dois POSTs do mesmo id que acharam o
        recibo de outro leem e decidem um de cada vez, e não decidem os
        dois que o lease venceu.
        """
        query = select(PixTransactionReceipt).where(PixTransactionReceipt.external_id == external_id)
        if for_update:
            query = query.with_for_update()
        return self.session.scalars(query).one_or_none()

    def create_receipt(
        self, *, external_id: str, payload_hash: str, received_at: datetime, owner_token: str, lease_until: datetime,
    ) -> PixTransactionReceipt | None:
        """Grava o recibo em processing e devolve a linha, ou None se o id já tem recibo.

        É a reivindicação: o INSERT ... ON CONFLICT (external_id) DO
        NOTHING espera outro INSERT do mesmo id terminar e, se ele
        commitou, desiste sem erro — e sem o IntegrityError, que iria pro
        log do Postgres com os valores. Quem recebe None lê o recibo do
        outro (get_receipt) e decide a partir do estado dele.
        """
        return self.session.scalars(
            insert(PixTransactionReceipt)
            .values(
                external_id=external_id,
                payload_hash=payload_hash,
                received_at=received_at,
                state=ReceiptState.PROCESSING.value,
                owner_token=owner_token,
                lease_until=lease_until,
            )
            .on_conflict_do_nothing(index_elements=[PixTransactionReceipt.external_id])
            .returning(PixTransactionReceipt)
        ).one_or_none()

    def reclaim_receipt(
        self, receipt: PixTransactionReceipt, *, owner_token: str, received_at: datetime, lease_until: datetime,
    ) -> bool:
        """Toma pra si um recibo failed, ou processing com o lease vencido: True se conseguiu.

        Compare-and-swap no owner_token: o UPDATE só pega a linha se ela
        ainda é a que foi lida (mesmo dono, não concluída). Quem perder a
        corrida recebe False e trata o recibo como de outro. Nunca um
        segundo recibo pro mesmo id (RFC §8).
        """
        result = self.session.execute(
            update(PixTransactionReceipt)
            .where(
                PixTransactionReceipt.id == receipt.id,
                PixTransactionReceipt.owner_token == receipt.owner_token,
                PixTransactionReceipt.state != ReceiptState.COMPLETED.value,
            )
            .values(
                state=ReceiptState.PROCESSING.value,
                owner_token=owner_token,
                received_at=received_at,
                lease_until=lease_until,
                evaluated_at=None,
                failure_reason=None,
            )
        )
        return result.rowcount == 1

    def complete_receipt(
        self, receipt_id: int, *, owner_token: str, pix_transaction_id: int, evaluated_at: datetime,
    ) -> bool:
        """Marca o recibo como completed, apontando pra análise: True se ainda éramos os donos.

        É a cerca (fencing) do owner_token: um avaliador que perdeu o
        lease pra outro recebe False aqui e desfaz tudo, em vez de gravar
        uma segunda análise por cima da do dono atual. Mesma transação do
        INSERT do Pix: ou os dois ficam, ou nenhum.
        """
        result = self.session.execute(
            update(PixTransactionReceipt)
            .where(
                PixTransactionReceipt.id == receipt_id,
                PixTransactionReceipt.owner_token == owner_token,
                PixTransactionReceipt.state == ReceiptState.PROCESSING.value,
            )
            .values(state=ReceiptState.COMPLETED.value, pix_transaction_id=pix_transaction_id, evaluated_at=evaluated_at)
        )
        return result.rowcount == 1

    def fail_receipt(self, receipt_id: int, *, owner_token: str, reason: str) -> bool:
        """Marca o recibo como failed, com o motivo: True se ainda éramos os donos.

        O motivo chega sanitizado do controller (nome da classe e
        descrição, nunca documento) e cabe em TEXT; o traceback inteiro
        fica no log.
        """
        result = self.session.execute(
            update(PixTransactionReceipt)
            .where(
                PixTransactionReceipt.id == receipt_id,
                PixTransactionReceipt.owner_token == owner_token,
                PixTransactionReceipt.state == ReceiptState.PROCESSING.value,
            )
            .values(state=ReceiptState.FAILED.value, failure_reason=reason)
        )
        return result.rowcount == 1

    def create(self, **fields) -> PixTransaction | None:
        """Grava o Pix e devolve o model com o id preenchido, ou None se o external_id já estava gravado.

        fields usa os nomes das colunas (status_id, payer_risk,
        pix_transaction_data...); só a pix_transaction_key nasce aqui. O
        INSERT sai agora, sem commit: os sinais precisam do id.

        Com o recibo na frente, só um avaliador por id chega aqui, e o
        None não devia acontecer; fica como cinto de segurança. O ON
        CONFLICT (external_id) DO NOTHING segura o nosso INSERT até outro
        do mesmo id terminar e, se ele commitou, desiste sem erro.
        Capturar o IntegrityError daria no mesmo, a um preço: o Postgres
        grava no log dele todo comando que falha, com os valores — o
        payload e o CPF sem máscara —, e o controller teria de conhecer o
        nome da constraint. Outra violação, um CHECK por exemplo, continua
        levantando.
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
