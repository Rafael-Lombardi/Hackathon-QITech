from datetime import datetime

from antifraud import Analysis, Context, Transaction, evaluate, policy
from connectors import DictConnector
from controllers.base_controller import BaseController
from controllers.pix_request import AccountData, PixRequest, parse_pix_request
from dtos import PixTransactionDTO
from errors import DictUnavailable, PixTransactionIdConflict, PixTransactionNotFound
from models import Account, Customer, PixTransaction
from repositories import AccountRepository, CustomerRepository, PixHistoryRepository, PixTransactionRepository
from utils.document_number import mask_document


class PixTransactionController(BaseController):
    """O caso de uso do Pix: recebe, decide, grava e devolve a análise.

    A regra de risco não mora aqui. Ela é o motor, em src/antifraud/, que
    recebe um Context pronto e devolve a decisão sem tocar em banco nem
    em rede. O que mora aqui é tudo em volta dele, na ordem certa:
    idempotência, DICT, trava, histórico, gravação e commit. É a única
    peça que fala com as outras três — os repositories (banco), o
    connector (DICT) e o motor.
    """

    def __init__(self) -> None:
        super().__init__(__name__)
        self.customer_repository = CustomerRepository(self.context)
        self.account_repository = AccountRepository(self.context)
        self.pix_transaction_repository = PixTransactionRepository(self.context)
        self.pix_history_repository = PixHistoryRepository(self.context)
        self.dict_connector = DictConnector()

    def create(self, payload: dict) -> tuple[dict, bool]:
        """Analisa um Pix novo, ou devolve a análise que ele já tem.

        Devolve o corpo da resposta e se a análise nasceu agora: o
        resource transforma isso em 201 ou 200.

        A ordem é a regra, e cada passo tem um porquê:

          1. converte o payload — data impossível é 400, antes de tudo;
          2. procura o id: reenvio idêntico devolve a análise gravada, id
             repetido com outro conteúdo é 409. Sem DICT, e sem contar
             nada de novo no histórico;
          3. pergunta ao DICT pelo recebedor ANTES de travar qualquer
             linha. A consulta pode levar os 2 s do prazo, e um pagador
             travado esse tempo todo seguraria todos os Pix dele;
          4. cria customers e contas na primeira aparição;
          5. trava o pagador — a linha do customer, porque as regras do
             pagador contam pelo documento, somando as contas dele — e
             procura o id de novo: outro POST do mesmo Pix pode ter gravado
             enquanto este esperava;
          6. conta o histórico, monta o Context e roda o motor;
          7. grava o Pix e os 19 sinais, e só então commita;
          8. a não ser que o mesmo id tenha chegado junto por outro
             pagador, que a trava não segura, e gravado primeiro: aí este
             responde como num reenvio, 200 ou 409.
        """
        pix_request = parse_pix_request(payload)

        replayed = self._replay_if_recorded(pix_request)
        if replayed is not None:
            return replayed, False

        # O rollback não desfaz nada — só houve um SELECT. Ele devolve a
        # conexão ao pool, o estoque de 15 conexões de banco que o processo
        # divide entre todas as requisições, pra outra usar enquanto este
        # Pix espera o DICT. Medido com 120 Pix e o DICT lento: 6 Pix/s
        # segurando a conexão, 12 devolvendo.
        self.session.rollback()
        trust_factor = self._get_trust_factor(pix_request.destination_account.document_number)

        payer, recipient = self._get_or_create_customers(pix_request)
        source_account = self._get_or_create_account(payer, pix_request.source_account)
        destination_account = self._get_or_create_account(recipient, pix_request.destination_account)

        self.customer_repository.lock_for_pix(customer_id=payer.id)

        replayed = self._replay_if_recorded(pix_request)
        if replayed is not None:
            return replayed, False

        analysis = evaluate(self._build_context(pix_request, payer, recipient, trust_factor))

        # A trava é por pagador, e não segura o mesmo id chegando junto por
        # DOIS pagadores. Esse caso o banco resolve: o INSERT ... ON
        # CONFLICT (external_id) DO NOTHING espera o outro commitar, desiste
        # sem erro e devolve None. Aí a análise do outro já está gravada, e
        # este responde como num reenvio — 200 ou 409, nunca 500.
        pix_transaction = self._create_pix_transaction(pix_request, analysis, source_account, destination_account)
        if pix_transaction is None:
            return self._replay_if_recorded(pix_request), False

        self.pix_transaction_repository.create_signals(
            pix_transaction_id=pix_transaction.id,
            signals=self._signal_rows(analysis),
        )

        self.logger.info(
            f"Pix {pix_request.external_id} de {mask_document(pix_request.source_account.document_number)} "
            f"para {mask_document(pix_request.destination_account.document_number)}: "
            f"{analysis.status} pela {analysis.decision_rule}"
        )

        pix_transaction_dto = self._recorded_analysis(pix_transaction)
        self.session.commit()

        return pix_transaction_dto, True

    def get_by_external_id(self, external_id: str) -> dict:
        """A análise gravada do Pix com este id do payload.

        Um %00 no caminho chega aqui como o caractere NUL, que o Postgres
        recusa até numa consulta — o GET daria 500. Nenhum id com NUL passa
        pelo schema do POST: não há o que achar.
        """
        if "\x00" in external_id:
            raise PixTransactionNotFound(external_id)

        pix_transaction = self.pix_transaction_repository.get_by_external_id(external_id=external_id)

        if pix_transaction is None:
            raise PixTransactionNotFound(external_id)

        return self._recorded_analysis(pix_transaction)

    def _replay_if_recorded(self, pix_request: PixRequest) -> dict | None:
        """A análise já gravada com este id, se houver — ou 409, se o conteúdo mudou.

        A comparação é do payload como chegou, dicionário contra
        dicionário. Chave em outra ordem é o mesmo Pix; o CPF agora com
        máscara não é — o que fica gravado é o que chegou, e um reenvio
        que mudou algum valor já é outro pedido.
        """
        recorded = self.pix_transaction_repository.get_by_external_id(external_id=pix_request.external_id)

        if recorded is None:
            return None

        if recorded.pix_transaction_data != pix_request.payload:
            raise PixTransactionIdConflict(pix_request.external_id)

        self.logger.info(f"Pix {pix_request.external_id} reenviado: devolvendo a análise gravada")

        return self._recorded_analysis(recorded)

    def _get_trust_factor(self, document_number: str) -> int | None:
        """O trust factor do recebedor no DICT, ou None se o DICT falhou.

        É aqui que "o DICT caiu" vira "a camada 3 não tem como responder":
        o motor recebe None, o DICT01 sai unavailable e o Pix é decidido
        pelas outras duas camadas. O porquê da falha o connector já deixou
        no log.
        """
        try:
            return self.dict_connector.get_trust_factor(document_number)
        except DictUnavailable:
            return None

    def _get_or_create_customers(self, pix_request: PixRequest) -> tuple[Customer, Customer]:
        """Os customers do pagador e do recebedor, criados na primeira aparição.

        Repare na ordem: a do número do documento, menor primeiro, e não
        pagador primeiro. Dois Pix cruzados de gente nova — A paga B
        enquanto B paga A — criariam os dois customers em ordens opostas,
        cada um esperando o INSERT do outro terminar: deadlock, e um dos
        dois vira 500. Na mesma ordem, o segundo só espera o primeiro
        commitar. Reproduzido com 100 pares cruzados: pagador primeiro, 73
        POSTs com 500; em ordem de documento, nenhum.

        Pagador e recebedor podem ser o mesmo documento (Pix pra si mesmo,
        o REL03), e aí é um customer só.
        """
        payer_document = pix_request.source_account.document_number
        recipient_document = pix_request.destination_account.document_number

        customers = {}
        for document_number in sorted({payer_document, recipient_document}):
            customers[document_number] = self.customer_repository.get_or_create(document_number=document_number)

        return customers[payer_document], customers[recipient_document]

    def _get_or_create_account(self, customer: Customer, account_data: AccountData) -> Account:
        return self.account_repository.get_or_create(
            customer_id=customer.id,
            participant=account_data.participant,
            branch=account_data.branch,
            account_number=account_data.account_number,
            opening_date=account_data.opening_date,
        )

    def _build_context(
        self, pix_request: PixRequest, payer: Customer, recipient: Customer, trust_factor: int | None,
    ) -> Context:
        """O Context do motor: o Pix normalizado, o histórico contado e o DICT.

        O histórico é contado por documento (o customer), com as janelas
        do policy.py: o repository conta, o motor decide. E é contado
        DEPOIS da trava — o Pix do mesmo pagador que chegou junto, e
        passou na frente, já está gravado e entra na contagem, venha ele
        da conta que vier.
        """
        transaction_date = pix_request.transaction_date

        payer_history = self.pix_history_repository.get_payer_features(
            payer_customer_id=payer.id,
            recipient_customer_id=recipient.id,
            device_id=pix_request.device_id,
            ip=pix_request.ip,
            transaction_date=transaction_date,
            attempts_window=policy.PAYER_ATTEMPTS_WINDOW,
            amount_window=policy.PAYER_AMOUNT_WINDOW,
            new_destinations_window=policy.NEW_DESTINATIONS_WINDOW,
        )
        device_history = self.pix_history_repository.get_device_features(
            device_id=pix_request.device_id,
            payer_customer_id=payer.id,
            transaction_date=transaction_date,
            payers_window=policy.DEVICE_PAYERS_WINDOW,
        )
        recipient_history = self.pix_history_repository.get_recipient_features(
            recipient_customer_id=recipient.id,
            payer_customer_id=payer.id,
            transaction_date=transaction_date,
            attempts_window=policy.RECIPIENT_ATTEMPTS_WINDOW,
            payers_window=policy.RECIPIENT_PAYERS_WINDOW,
            attempts_cap=policy.HIGH_SUFFICIENCY_MIN_ATTEMPTS,
            days_cap=policy.HIGH_SUFFICIENCY_MIN_DAYS,
        )
        pair_history = self.pix_history_repository.get_pair_features(
            payer_customer_id=payer.id,
            recipient_customer_id=recipient.id,
            transaction_date=transaction_date,
            anomaly_window=policy.PAIR_ANOMALY_WINDOW,
        )

        transaction = Transaction(
            amount=pix_request.amount,
            transaction_date=transaction_date,
            payer_document=pix_request.source_account.document_number,
            recipient_document=pix_request.destination_account.document_number,
            payer_account_age_days=self._account_age_days(pix_request.source_account, transaction_date),
            recipient_account_age_days=self._account_age_days(pix_request.destination_account, transaction_date),
        )

        return Context(
            transaction=transaction,
            payer=payer_history,
            device=device_history,
            recipient=recipient_history,
            pair=pair_history,
            trust_factor=trust_factor,
        )

    def _account_age_days(self, account_data: AccountData, transaction_date: datetime) -> int:
        """A idade da conta no instante do Pix, em dias de 24 h.

        A abertura é a do PAYLOAD, e não a gravada na primeira aparição da
        conta: o P01 da RFC olha "só a data de abertura informada". Conta
        aberta depois do Pix é dado inconsistente, e vira 0 — tratar como
        conta nova é o lado conservador, sem inventar um 400 que a RFC não
        prevê.
        """
        return max((transaction_date - account_data.opening_date).days, 0)

    def _create_pix_transaction(
        self, pix_request: PixRequest, analysis: Analysis, source_account: Account, destination_account: Account,
    ) -> PixTransaction | None:
        return self.pix_transaction_repository.create(
            status_id=self.pix_transaction_repository.get_status_id(enumerator=analysis.status),
            payer_risk=analysis.payer_risk,
            recipient_risk=analysis.recipient_risk,
            payer_sufficiency=analysis.payer_sufficiency,
            recipient_sufficiency=analysis.recipient_sufficiency,
            policy_version=analysis.policy_version,
            decision_rule=analysis.decision_rule,
            reasons=list(analysis.reasons),
            source_account_id=source_account.id,
            destination_account_id=destination_account.id,
            pix_transaction_data=pix_request.payload,
            external_id=pix_request.external_id,
            amount=pix_request.amount,
            transaction_date=pix_request.transaction_date,
            device_id=pix_request.device_id,
            channel=pix_request.channel,
            ip=pix_request.ip,
            latitude=pix_request.latitude,
            longitude=pix_request.longitude,
        )

    def _signal_rows(self, analysis: Analysis) -> list[dict]:
        """Os 19 sinais como linhas de pix_transaction_rule_result.

        Vão todos, inclusive os not_triggered e os unavailable: o GET
        mostra cada regra e o que ela disse, até quando foi "não sei".
        """
        return [
            {
                "rule_name": signal.code,
                "signal_status_id": self.pix_transaction_repository.get_signal_status_id(enumerator=signal.status),
                "axis": signal.axis,
                "family": signal.family,
                "points": signal.points,
                "rule_result_data": signal.evidence,
            }
            for signal in analysis.signals
        ]

    def _recorded_analysis(self, pix_transaction: PixTransaction) -> dict:
        rule_results = self.pix_transaction_repository.list_signals(pix_transaction_id=pix_transaction.id)

        return PixTransactionDTO.obj_to_dict(pix_transaction, rule_results)
