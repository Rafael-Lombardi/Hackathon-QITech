import json
import time
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from sqlalchemy.exc import SQLAlchemyError

from antifraud import Analysis, Context, Transaction, evaluate, policy
from connectors import DictConnector
from controllers.base_controller import BaseController
from controllers.pix_request import AccountData, PixRequest, parse_pix_request
from dtos import PixTransactionDTO
from errors import DictUnavailable, PixTransactionIdConflict, PixTransactionNotFound
from models import Account, Customer, PixTransaction
from repositories import (
    AccountRepository,
    CustomerRepository,
    DeviceRegistryRepository,
    PixHistoryRepository,
    PixTransactionRepository,
    RiskRepository,
)
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
        self.device_registry_repository = DeviceRegistryRepository(self.context)
        self.risk_repository = RiskRepository(self.context)
        self.dict_connector = DictConnector()

    def create(self, payload: dict, *, test_received_at: datetime | None = None) -> tuple[dict, bool]:
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
          7. grava o Pix e os 22 sinais, e só então commita;
          8. a não ser que o mesmo id tenha chegado junto por outro
             pagador, que a trava não segura, e gravado primeiro: aí este
             responde como num reenvio, 200 ou 409.
        """
        pix_request = parse_pix_request(payload)

        replayed = self._replay_if_recorded(pix_request)
        if replayed is not None:
            return replayed, False

        received_at = test_received_at or datetime.now(timezone.utc)
        receipt, claimed = self.pix_transaction_repository.claim_receipt(
            external_id=pix_request.external_id, payload=pix_request.payload, now=received_at,
        )
        if not claimed:
            if receipt.payload != pix_request.payload:
                raise PixTransactionIdConflict(pix_request.external_id)
            # A completed receipt normally has a transaction and was caught by
            # the replay above. An active claim is deliberately not a verdict.
            if receipt.pix_transaction_id is None:
                replayed = self._wait_for_replay(pix_request)
                if replayed is not None:
                    return replayed, False
                raise PixTransactionIdConflict(pix_request.external_id)
            replayed = self._replay_if_recorded(pix_request)
            if replayed is not None:
                return replayed, False
            raise PixTransactionIdConflict(pix_request.external_id)
        # Make the claim durable before external I/O or risk evaluation.
        self.session.commit()

        try:
            return self._process_claimed(pix_request, received_at, receipt), True
        except Exception as error:
            self.session.rollback()
            failed = self.pix_transaction_repository.get_receipt(pix_request.external_id)
            if failed is not None:
                self.pix_transaction_repository.mark_receipt_failed(failed, str(error))
                self.session.commit()
            raise

    def _wait_for_replay(self, pix_request: PixRequest) -> dict | None:
        """Wait briefly for the owner of an active receipt to commit."""
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            self.session.rollback()
            replayed = self._replay_if_recorded(pix_request)
            if replayed is not None:
                return replayed
            time.sleep(0.01)
        return None

    def _process_claimed(self, pix_request: PixRequest, received_at: datetime, receipt) -> dict:

        # O rollback não desfaz nada — só houve um SELECT. Ele devolve a
        # conexão ao pool, o estoque de 15 conexões de banco que o processo
        # divide entre todas as requisições, pra outra usar enquanto este
        # Pix espera o DICT. Medido com 120 Pix e o DICT lento: 6 Pix/s
        # segurando a conexão, 12 devolvendo.
        self.session.rollback()
        dict_security_statistics = self._get_dict_security_statistics(
            pix_request.destination_account.document_number
        )

        self.customer_repository.lock_for_pix_document(
            document_number=pix_request.source_account.document_number
        )
        payer, recipient = self._get_or_create_customers(pix_request)
        source_account = self._get_or_create_account(payer, pix_request.source_account)
        destination_account = self._get_or_create_account(recipient, pix_request.destination_account)

        self.customer_repository.lock_for_pix_many(customer_ids=(payer.id, recipient.id))

        replayed = self._replay_if_recorded(pix_request)
        if replayed is not None:
            return replayed

        context, risk_evidence, context_snapshot = self._build_context(
            pix_request, payer, recipient, dict_security_statistics, destination_account.id, received_at
        )
        analysis = evaluate(context)

        # A trava é por pagador, e não segura o mesmo id chegando junto por
        # DOIS pagadores. Esse caso o banco resolve: o INSERT ... ON
        # CONFLICT (external_id) DO NOTHING espera o outro commitar, desiste
        # sem erro e devolve None. Aí a análise do outro já está gravada, e
        # este responde como num reenvio — 200 ou 409, nunca 500.
        evaluated_at = datetime.now(timezone.utc)
        pix_transaction = self._create_pix_transaction(
            pix_request, analysis, source_account, destination_account,
            received_at=received_at, evaluated_at=evaluated_at, evaluation_context=context_snapshot,
        )
        if pix_transaction is None:
            replayed = self._replay_if_recorded(pix_request)
            if replayed is None:
                raise PixTransactionIdConflict(pix_request.external_id)
            return replayed

        self.pix_transaction_repository.create_signals(
            pix_transaction_id=pix_transaction.id,
            signals=self._signal_rows(analysis),
        )
        self.pix_transaction_repository.create_risk_evidence(
            pix_transaction_id=pix_transaction.id,
            evidence=risk_evidence,
        )

        self.logger.info(
            f"Pix {pix_request.external_id} de {mask_document(pix_request.source_account.document_number)} "
            f"para {mask_document(pix_request.destination_account.document_number)}: "
            f"{analysis.status} pela {analysis.decision_rule}"
        )

        pix_transaction_dto = self._recorded_analysis(pix_transaction)
        self.pix_transaction_repository.mark_receipt_completed(receipt, pix_transaction.id, evaluated_at)
        self.session.commit()

        return pix_transaction_dto

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

    def _get_dict_security_statistics(self, document_number: str) -> int | None:
        """Indicador mockado das estatísticas DICT, ou None se falhou.

        É aqui que "o DICT caiu" vira "a camada 3 não tem como responder":
        o motor recebe None, o DICT01 sai unavailable e o Pix é decidido
        pelas outras duas camadas. O porquê da falha o connector já deixou
        no log.
        """
        try:
            return self.dict_connector.get_security_statistics(document_number)
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
        self,
        pix_request: PixRequest,
        payer: Customer,
        recipient: Customer,
        dict_security_statistics: int | None,
        recipient_account_id: int,
        received_at: datetime,
    ) -> tuple[Context, list[dict], dict]:
        """O Context do motor: o Pix normalizado, o histórico contado e o DICT.

        O histórico é contado por documento (o customer), com as janelas
        do policy.py: o repository conta, o motor decide. E é contado
        DEPOIS da trava — o Pix do mesmo pagador que chegou junto, e
        passou na frente, já está gravado e entra na contagem, venha ele
        da conta que vier.
        """
        transaction_date = pix_request.transaction_date
        # Histórico e janelas antifraude usam o relógio confiável do servidor.
        # Em duas requisições concorrentes com o mesmo instante de negócio,
        # porém, a que adquire o lock primeiro pode ter recebido um timestamp
        # de teste ligeiramente maior. A folga de um segundo inclui essa
        # mesma batelada, sem alterar o caminho normal, em que received_at já
        # é posterior ao instante declarado.
        history_time = max(received_at, transaction_date + timedelta(seconds=1))

        payer_history = self.pix_history_repository.get_payer_features(
            payer_customer_id=payer.id,
            recipient_customer_id=recipient.id,
            device_id=pix_request.device_id,
            ip=pix_request.ip,
            transaction_date=history_time,
            attempts_window=policy.PAYER_ATTEMPTS_WINDOW,
            amount_window=policy.PAYER_AMOUNT_WINDOW,
            new_destinations_window=policy.NEW_DESTINATIONS_WINDOW,
        )
        location_distance_km = self.pix_history_repository.get_location_distance_km(
            payer_customer_id=payer.id,
            transaction_date=history_time,
            latitude=pix_request.latitude,
            longitude=pix_request.longitude,
        )
        device_history = self.pix_history_repository.get_device_features(
            device_id=pix_request.device_id,
            payer_customer_id=payer.id,
            transaction_date=history_time,
            payers_window=policy.DEVICE_PAYERS_WINDOW,
        )
        recipient_history = self.pix_history_repository.get_recipient_features(
            recipient_customer_id=recipient.id,
            payer_customer_id=payer.id,
            transaction_date=history_time,
            attempts_window=policy.RECIPIENT_ATTEMPTS_WINDOW,
            payers_window=policy.RECIPIENT_PAYERS_WINDOW,
            attempts_cap=policy.HIGH_SUFFICIENCY_MIN_ATTEMPTS,
            days_cap=policy.HIGH_SUFFICIENCY_MIN_DAYS,
        )
        pair_history = self.pix_history_repository.get_pair_features(
            payer_customer_id=payer.id,
            recipient_customer_id=recipient.id,
            transaction_date=history_time,
            anomaly_window=policy.PAIR_ANOMALY_WINDOW,
        )
        try:
            device_trusted = self.device_registry_repository.is_trusted(
                customer_id=payer.id,
                device_id=pix_request.device_id,
                at=history_time,
            )
            unregistered_device_day_amount = self.device_registry_repository.unregistered_device_day_amount(
                customer_id=payer.id,
                at=history_time,
            )
            device_registry_available = True
        except SQLAlchemyError:
            # Cadastro obrigatório indisponível: o motor pede 2FA, nunca
            # transforma falta de fonte em dispositivo não confiável.
            device_trusted = None
            unregistered_device_day_amount = 0
            device_registry_available = False
        payer_history = replace(
            payer_history,
            unregistered_device_day_amount=unregistered_device_day_amount,
        )

        transaction = Transaction(
            amount=pix_request.amount,
            transaction_date=transaction_date,
            payer_document=pix_request.source_account.document_number,
            recipient_document=pix_request.destination_account.document_number,
            payer_account_age_days=self._account_age_days(pix_request.source_account, transaction_date),
            recipient_account_age_days=self._account_age_days(pix_request.destination_account, transaction_date),
            latitude=pix_request.latitude,
            longitude=pix_request.longitude,
            location_distance_km=location_distance_km,
        )

        risk_inputs = self._get_risk_inputs(
            payer_customer_id=payer.id,
            recipient_customer_id=recipient.id,
            recipient_account_id=recipient_account_id,
            transaction_date=history_time,
            captured_at=received_at,
        )

        context = Context(
            transaction=transaction,
            payer=payer_history,
            device=device_history,
            recipient=recipient_history,
            pair=pair_history,
            dict_security_statistics=dict_security_statistics,
            device_trusted=device_trusted,
            device_registry_available=device_registry_available,
            restriction_active=risk_inputs["restriction_active"],
            restriction_available=risk_inputs["restriction_available"],
            payer_external_adverse=risk_inputs["payer_external_adverse"],
            payer_external_confirmed=risk_inputs["payer_external_confirmed"],
            recipient_external_adverse=risk_inputs["recipient_external_adverse"],
            recipient_external_confirmed=risk_inputs["recipient_external_confirmed"],
            payer_dict_security_assessment=risk_inputs["payer_dict_security_assessment"],
            recipient_dict_security_assessment=risk_inputs["recipient_dict_security_assessment"],
        )
        snapshot = json.loads(json.dumps(asdict(context), default=str))
        snapshot["history_evaluated_at"] = history_time.isoformat()
        return context, risk_inputs["evidence"], snapshot

    def _get_risk_inputs(
        self,
        *,
        payer_customer_id: int,
        recipient_customer_id: int,
        recipient_account_id: int,
        transaction_date: datetime,
        captured_at: datetime,
    ) -> dict:
        """Lê DICT security evidence uma vez e congela flags para o motor."""
        try:
            restriction = self.risk_repository.active_restriction(
                payer_customer_id=payer_customer_id,
                recipient_customer_id=recipient_customer_id,
                recipient_account_id=recipient_account_id,
                at=transaction_date,
            )
            payer_assessment = self.risk_repository.dict_security_evidence(
                customer_id=payer_customer_id, axis="payer", at=transaction_date,
            )
            recipient_assessment = self.risk_repository.dict_security_evidence(
                customer_id=recipient_customer_id, axis="recipient", at=transaction_date,
            )
        except SQLAlchemyError:
            return {
                "restriction_active": False,
                "restriction_available": False,
                "payer_external_adverse": False,
                "payer_external_confirmed": False,
                "recipient_external_adverse": False,
                "recipient_external_confirmed": False,
                "payer_dict_security_assessment": None,
                "recipient_dict_security_assessment": None,
                "evidence": [],
            }

        def flags(assessment) -> tuple[bool, bool]:
            if assessment is None:
                return False, False
            confirmed = assessment.assessment == "confirmed_in_mock"
            adverse = confirmed or assessment.assessment == "suspicious"
            return adverse, confirmed

        payer_adverse, payer_confirmed = flags(payer_assessment)
        recipient_adverse, recipient_confirmed = flags(recipient_assessment)
        evidence = []
        if restriction is not None:
            evidence.append({
                "source_type": "restriction",
                "scope": restriction.scope,
                "assessment": None,
                "axis": None,
                "provenance": restriction.provenance,
                "occurrence_id": restriction.occurrence_id,
                "valid_from": restriction.valid_from,
                "valid_until": restriction.valid_until,
                "captured_at": captured_at,
                "severity": None,
                "source_version": None,
                "observed_at": None,
            })
        for assessment in (payer_assessment, recipient_assessment):
            if assessment is not None:
                evidence.append({
                "source_type": "dict_security",
                    "scope": None,
                    "assessment": assessment.assessment,
                    "axis": assessment.axis,
                    "provenance": assessment.provenance,
                    "occurrence_id": assessment.occurrence_id,
                    "valid_from": assessment.valid_from,
                    "valid_until": assessment.valid_until,
                    "captured_at": captured_at,
                    "severity": assessment.severity,
                    "source_version": assessment.source_version,
                    "observed_at": assessment.observed_at,
                })
        return {
            "restriction_active": restriction is not None,
            "restriction_available": True,
            "payer_external_adverse": payer_adverse,
            "payer_external_confirmed": payer_confirmed,
            "recipient_external_adverse": recipient_adverse,
            "recipient_external_confirmed": recipient_confirmed,
            "payer_dict_security_assessment": payer_assessment.assessment if payer_assessment else None,
            "recipient_dict_security_assessment": recipient_assessment.assessment if recipient_assessment else None,
            "evidence": evidence,
        }

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
        *, received_at: datetime, evaluated_at: datetime, evaluation_context: dict,
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
            received_at=received_at,
            evaluated_at=evaluated_at,
            evaluation_context=evaluation_context,
        )

    def _signal_rows(self, analysis: Analysis) -> list[dict]:
        """Os 20 sinais como linhas de pix_transaction_rule_result.

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
