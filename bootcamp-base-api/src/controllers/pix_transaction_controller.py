from datetime import datetime, timezone

from antifraud import Analysis, Context, DeviceRegistration, Restriction, Transaction, evaluate, policy
from connectors import DeviceRegistryConnector, DictConnector, RestrictionsConnector
from connectors.device_registry_connector import DeviceRegistryRecord
from connectors.dict_connector import DictSecurityInformation
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
    idempotência, fontes, trava, histórico, gravação e commit. É a única
    peça que fala com as outras três — os repositories (banco), os
    connectors (as fontes de fora) e o motor.
    """

    def __init__(self) -> None:
        super().__init__(__name__)
        self.customer_repository = CustomerRepository(self.context)
        self.account_repository = AccountRepository(self.context)
        self.pix_transaction_repository = PixTransactionRepository(self.context)
        self.pix_history_repository = PixHistoryRepository(self.context)
        self.restrictions_connector = RestrictionsConnector()
        self.device_registry_connector = DeviceRegistryConnector()
        self.dict_connector = DictConnector()

    def create(self, payload: dict) -> tuple[dict, bool]:
        """Analisa um Pix novo, ou devolve a análise que ele já tem.

        Devolve o corpo da resposta e se a análise nasceu agora: o
        resource transforma isso em 201 ou 200.

        A ordem é a regra, e cada passo tem um porquê:

          1. converte o payload — data impossível é 400, antes de tudo;
          2. procura o id: reenvio idêntico devolve a análise gravada, id
             repetido com outro conteúdo é 409. Sem consultar fonte
             nenhuma, e sem contar nada de novo no histórico;
          3. consulta as três fontes ANTES de travar qualquer coisa: as
             restrições primeiro, porque sem elas não há veredito (503, e
             nada gravado), depois o cadastro de aparelhos e o DICT. Cada
             consulta pode levar os 2 s do prazo, e um pagador travado
             esse tempo todo seguraria todos os Pix dele;
          4. trava o pagador — pelo documento, porque as regras do
             pagador contam pelo documento, somando as contas dele — e só
             então cria customers e contas na primeira aparição;
          5. procura o id de novo: outro POST do mesmo Pix pode ter gravado
             enquanto este esperava a trava;
          6. conta o histórico, lê o que as fontes responderam no instante
             da avaliação, monta o Context e roda o motor;
          7. grava o Pix e os sinais, e só então commita — a não ser que o
             mesmo id tenha chegado junto por outro pagador, que a trava
             não segura, e gravado primeiro: aí este responde como num
             reenvio, 200 ou 409.
        """
        pix_request = parse_pix_request(payload)

        replayed = self._replay_if_recorded(pix_request)
        if replayed is not None:
            return replayed, False

        # O rollback não desfaz nada — só houve um SELECT. Ele devolve a
        # conexão ao pool, o estoque de conexões de banco que o processo
        # divide entre todas as requisições, pra outra usar enquanto este
        # Pix espera as fontes: com uma fonte lenta, segurar a conexão na
        # espera derrubava a vazão pela metade.
        self.session.rollback()
        restrictions = self.restrictions_connector.list_restrictions(
            payer_document=pix_request.source_account.document_number,
            recipient_document=pix_request.destination_account.document_number,
            recipient_account_key=_account_key(pix_request.destination_account),
        )
        registry_record = self.device_registry_connector.get_registration(
            pix_request.source_account.document_number, pix_request.device_id,
        )
        dict_information = self._get_dict_security_information(
            pix_request.destination_account.document_number
        )

        self.customer_repository.lock_for_pix_document(
            document_number=pix_request.source_account.document_number
        )
        payer, recipient = self._get_or_create_customers(pix_request)
        source_account = self._get_or_create_account(payer, pix_request.source_account)
        destination_account = self._get_or_create_account(recipient, pix_request.destination_account)

        replayed = self._replay_if_recorded(pix_request)
        if replayed is not None:
            return replayed, False

        context = self._build_context(pix_request, payer, recipient, registry_record, restrictions, dict_information)
        analysis = evaluate(context)

        # A trava é por pagador, e não segura o mesmo id chegando junto por
        # DOIS pagadores. Esse caso o banco resolve: o INSERT ... ON
        # CONFLICT (external_id) DO NOTHING espera o outro commitar, desiste
        # sem erro e devolve None. Aí a análise do outro já está gravada, e
        # este responde como num reenvio — 200 ou 409, nunca 500.
        #
        # received_at é só o carimbo de chegada, pra auditoria: as janelas
        # de histórico contam por transaction_date (pix_history_repository).
        pix_transaction = self._create_pix_transaction(
            pix_request, analysis, context, source_account, destination_account,
            received_at=datetime.now(timezone.utc),
        )
        if pix_transaction is None:
            replayed = self._replay_if_recorded(pix_request)
            if replayed is None:
                raise PixTransactionIdConflict(pix_request.external_id)
            return replayed, False

        self.pix_transaction_repository.create_signals(
            pix_transaction_id=pix_transaction.id,
            signals=self._signal_rows(analysis),
        )

        self.logger.info(
            f"Pix {pix_request.external_id} de {mask_document(pix_request.source_account.document_number)} "
            f"para {mask_document(pix_request.destination_account.document_number)}: "
            f"{analysis.status} pela {analysis.decision_rule}"
        )

        self.session.commit()

        return self._recorded_analysis(pix_transaction), True

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

    def _get_dict_security_information(self, document_number: str) -> DictSecurityInformation | None:
        """A estatística e a marcação de fraude do DICT, ou None se falhou.

        É aqui que "o DICT caiu" vira "a camada 3 não tem como responder":
        o motor recebe None, o DICT01, o DICT02 e o DICT03 saem unavailable
        e o Pix é decidido pelas outras duas camadas. O porquê da falha o
        connector já deixou no log.
        """
        try:
            return self.dict_connector.get_security_information(document_number)
        except DictUnavailable:
            return None

    def _get_or_create_customers(self, pix_request: PixRequest) -> tuple[Customer, Customer]:
        """Os customers do pagador e do recebedor, criados na primeira aparição.

        Repare na ordem: a do número do documento, menor primeiro, e não
        pagador primeiro. Dois Pix cruzados de gente nova — A paga B
        enquanto B paga A — criariam os dois customers em ordens opostas,
        cada um esperando o INSERT do outro terminar: deadlock, e um dos
        dois vira 500 (reproduzido). Na mesma ordem, o segundo só espera o
        primeiro commitar.

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
        self,
        pix_request: PixRequest,
        payer: Customer,
        recipient: Customer,
        registry_record: DeviceRegistryRecord,
        restrictions: tuple[Restriction, ...],
        dict_information: DictSecurityInformation | None,
    ) -> Context:
        """O Context do motor: o Pix normalizado, o histórico contado e o que as fontes disseram.

        O histórico é contado por documento (o customer), com as janelas
        do policy.py e o relógio do negócio, transaction_date (o porquê
        está no pix_history_repository.py): o repository conta, o motor
        decide. E é contado DEPOIS da trava — o Pix do mesmo pagador que
        chegou junto, e passou na frente, já está gravado e entra na
        contagem, venha ele da conta que vier. As contas dos dois
        documentos vão prontas pras consultas do pagador e do par; o
        porquê está no AccountRepository.ids_of.

        As fontes foram consultadas antes da trava, e o que elas
        responderam é lido AQUI, no instante da avaliação: o cadastro do
        aparelho tem vigência, a restrição também, e as duas se resolvem
        contra o mesmo relógio do histórico.
        """
        transaction_date = pix_request.transaction_date
        accounts = self.account_repository.ids_of(customer_ids={payer.id, recipient.id})

        payer_history = self.pix_history_repository.get_payer_features(
            payer_accounts=accounts[payer.id],
            recipient_customer_id=recipient.id,
            device_id=pix_request.device_id,
            ip=pix_request.ip,
            at=transaction_date,
            attempts_window=policy.PAYER_ATTEMPTS_WINDOW,
            amount_window=policy.PAYER_AMOUNT_WINDOW,
            new_destinations_window=policy.NEW_DESTINATIONS_WINDOW,
        )
        location_distance_km = self.pix_history_repository.get_location_distance_km(
            payer_customer_id=payer.id,
            at=transaction_date,
            latitude=pix_request.latitude,
            longitude=pix_request.longitude,
        )
        device_history = self.pix_history_repository.get_device_features(
            device_id=pix_request.device_id,
            payer_customer_id=payer.id,
            at=transaction_date,
            payers_window=policy.DEVICE_PAYERS_WINDOW,
        )
        recipient_history = self.pix_history_repository.get_recipient_features(
            recipient_customer_id=recipient.id,
            payer_customer_id=payer.id,
            at=transaction_date,
            attempts_window=policy.RECIPIENT_ATTEMPTS_WINDOW,
            payers_window=policy.RECIPIENT_PAYERS_WINDOW,
            attempts_cap=policy.HIGH_SUFFICIENCY_MIN_ATTEMPTS,
            days_cap=policy.HIGH_SUFFICIENCY_MIN_DAYS,
        )
        pair_history = self.pix_history_repository.get_pair_features(
            payer_accounts=accounts[payer.id],
            recipient_accounts=accounts[recipient.id],
            at=transaction_date,
            anomaly_window=policy.PAIR_ANOMALY_WINDOW,
        )

        transaction = Transaction(
            amount=pix_request.amount,
            transaction_date=transaction_date,
            payer_document=pix_request.source_account.document_number,
            recipient_document=pix_request.destination_account.document_number,
            payer_account_age_days=self._account_age_days(pix_request.source_account, transaction_date),
            recipient_account_age_days=self._account_age_days(pix_request.destination_account, transaction_date),
            location_distance_km=location_distance_km,
        )

        device_registration = DeviceRegistration(
            status=registry_record.status_at(transaction_date),
            source=registry_record.provenance,
            previously_observed=payer_history.device_previously_observed,
        )
        # A fonte já foi perguntada pelos três (pagador, titular e conta
        # exata); o filtro de alcance é a conferência de que ela respondeu
        # o que foi perguntado, e o de vigência é o que só se resolve aqui.
        active_restrictions = tuple(
            restriction for restriction in restrictions
            if restriction.applies_to(
                payer_document=transaction.payer_document,
                recipient_document=transaction.recipient_document,
                recipient_account_key=_account_key(pix_request.destination_account),
            ) and restriction.active_at(transaction_date)
        )

        return Context(
            transaction=transaction,
            payer=payer_history,
            device=device_history,
            recipient=recipient_history,
            pair=pair_history,
            device_registration=device_registration,
            restrictions=active_restrictions,
            dict_security_statistics=None if dict_information is None else dict_information.statistics,
            dict_fraud_marker=None if dict_information is None else dict_information.fraud_marker,
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
        self, pix_request: PixRequest, analysis: Analysis, context: Context,
        source_account: Account, destination_account: Account, *, received_at: datetime,
    ) -> PixTransaction | None:
        device_registration = context.device_registration
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
            device_registration_status=device_registration.status.value,
            device_registration_source=device_registration.source,
            device_previously_observed=device_registration.previously_observed,
            restrictions=[_restriction_row(restriction) for restriction in context.restrictions],
            received_at=received_at,
        )

    def _signal_rows(self, analysis: Analysis) -> list[dict]:
        """Os sinais do catálogo como linhas de pix_transaction_rule_result.

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


def _account_key(account_data: AccountData) -> str:
    """A conta exata como a fonte de restrições a identifica: ispb:agência:conta."""
    return f"{account_data.participant}:{account_data.branch}:{account_data.account_number}"


def _restriction_row(restriction: Restriction) -> dict:
    """A restrição como vai pra coluna e pra resposta: tudo menos a identity_key, que é documento."""
    return {
        "restriction_id": restriction.restriction_id,
        "scope": restriction.scope.value,
        "reason": restriction.reason,
        "evidence_ref": restriction.evidence_ref,
        "provenance": restriction.provenance,
        "effective_at": restriction.effective_at.isoformat(),
        "expires_at": None if restriction.expires_at is None else restriction.expires_at.isoformat(),
        "revoked_at": None if restriction.revoked_at is None else restriction.revoked_at.isoformat(),
    }
