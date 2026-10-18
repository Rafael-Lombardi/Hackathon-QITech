import json
from dataclasses import asdict, replace
from datetime import datetime, timezone
from sqlalchemy.exc import SQLAlchemyError

from antifraud import Analysis, Context, Transaction, evaluate, policy, signal_spec
from connectors import DictConnector
from controllers.base_controller import BaseController
from controllers.pix_request import AccountData, PixRequest, parse_pix_request
from dtos import PixTransactionDTO
from errors import (
    DictUnavailable,
    InvalidSchema,
    PixTransactionIdConflict,
    PixTransactionNotFound,
)
from models import Account, PixTransaction
from repositories import (
    AccountRepository,
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
          4. cria as contas (a conta carrega também o documento) na primeira aparição;
          5. trava o documento do pagador e depois as contas envolvidas — e
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
        self._validate_transaction_date(pix_request.transaction_date, received_at)
        return self._process_new(pix_request, received_at)

    def _process_new(
        self, pix_request: PixRequest, received_at: datetime,
    ) -> tuple[dict, bool]:

        # O rollback não desfaz nada — só houve um SELECT. Ele devolve a
        # conexão ao pool, o estoque de 15 conexões de banco que o processo
        # divide entre todas as requisições, pra outra usar enquanto este
        # Pix espera o DICT. Medido com 120 Pix e o DICT lento: 6 Pix/s
        # segurando a conexão, 12 devolvendo.
        self.session.rollback()
        dict_security_statistics = self._get_dict_security_statistics(
            pix_request.destination_account.document_number
        )

        self.account_repository.lock_for_pix_document(
            document_number=pix_request.source_account.document_number
        )
        source_account, destination_account = self._get_or_create_accounts(pix_request)

        self.account_repository.lock_for_pix_many(account_ids=(source_account.id, destination_account.id))

        evaluated_at = self.pix_history_repository.next_evaluation_time(
            payer_account_id=source_account.id,
            recipient_account_id=destination_account.id,
            requested_at=received_at,
        )

        replayed = self._replay_if_recorded(pix_request)
        if replayed is not None:
            return replayed, False

        context, risk_evidence, context_snapshot = self._build_context(
            pix_request, source_account, destination_account, dict_security_statistics, destination_account.id, evaluated_at
        )
        analysis = evaluate(context)
        analysis_metadata = {
            "evaluation_context": context_snapshot,
            "family_contributions": analysis.family_contributions,
            "signals": self._signal_rows(analysis),
            "risk_evidence": json.loads(json.dumps(risk_evidence, default=str)),
        }

        # A trava é por pagador, e não segura o mesmo id chegando junto por
        # DOIS pagadores. O banco ainda protege o INSERT com UNIQUE(external_id):
        # se outra avaliação ganhar a corrida, o INSERT devolve None e este
        # responde como reenvio — 200 ou 409.
        pix_transaction = self._create_pix_transaction(
            pix_request, analysis, source_account, destination_account,
            received_at=received_at,
        )
        if pix_transaction is None:
            replayed = self._replay_if_recorded(pix_request)
            if replayed is None:
                raise PixTransactionIdConflict(pix_request.external_id)
            return replayed, False

        persisted_analysis = self.pix_transaction_repository.create_analysis(
            pix_transaction_id=pix_transaction.id,
            status_id=self.pix_transaction_repository.get_status_id(enumerator=analysis.status),
            payer_risk=analysis.payer_risk,
            recipient_risk=analysis.recipient_risk,
            payer_sufficiency=analysis.payer_sufficiency,
            recipient_sufficiency=analysis.recipient_sufficiency,
            policy_version=analysis.policy_version,
            decision_rule=analysis.decision_rule,
            reasons=list(analysis.reasons),
            evaluated_at=evaluated_at,
            analysis_metadata=analysis_metadata,
        )
        pix_transaction.analysis = persisted_analysis

        self.logger.info(
            f"Pix {pix_request.external_id} de {mask_document(pix_request.source_account.document_number)} "
            f"para {mask_document(pix_request.destination_account.document_number)}: "
            f"{analysis.status} pela {analysis.decision_rule}"
        )

        self.session.commit()
        # Read back through the same path as GET. PostgreSQL canonicalizes
        # JSONB key order; returning the persisted row keeps POST and GET
        # byte-for-byte identical.
        recorded = self.pix_transaction_repository.get_by_external_id(
            external_id=pix_request.external_id
        )
        if recorded is None:
            raise PixTransactionIdConflict(pix_request.external_id)
        return self._recorded_analysis(recorded), True

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

    def _validate_transaction_date(self, transaction_date: datetime, received_at: datetime) -> None:
        skew = abs(transaction_date - received_at)
        if skew > policy.TRANSACTION_DATE_MAX_SKEW:
            minutes = int(policy.TRANSACTION_DATE_MAX_SKEW.total_seconds() // 60)
            raise InvalidSchema(
                f"transaction_date must be within {minutes} minutes of the server receipt time"
            )

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

    def _get_or_create_accounts(self, pix_request: PixRequest) -> tuple[Account, Account]:
        """Cria/recupera as duas contas em ordem determinística."""
        source = pix_request.source_account
        destination = pix_request.destination_account
        specs = [
            (source.document_number, source),
            (destination.document_number, destination),
        ]
        accounts: dict[tuple, Account] = {}
        for document_number, account_data in sorted(
            specs,
            key=lambda item: (item[0], item[1].participant or "", item[1].branch, item[1].account_number),
        ):
            key = (document_number, account_data.participant, account_data.branch, account_data.account_number)
            accounts[key] = self.account_repository.get_or_create(
                document_number=document_number,
                participant=account_data.participant,
                branch=account_data.branch,
                account_number=account_data.account_number,
                opening_date=account_data.opening_date,
            )
        source_key = (source.document_number, source.participant, source.branch, source.account_number)
        destination_key = (destination.document_number, destination.participant, destination.branch, destination.account_number)
        return accounts[source_key], accounts[destination_key]

    def _build_context(
        self,
        pix_request: PixRequest,
        payer: Account,
        recipient: Account,
        dict_security_statistics: int | None,
        recipient_account_id: int,
        received_at: datetime,
    ) -> tuple[Context, list[dict], dict]:
        """O Context do motor: o Pix normalizado, o histórico contado e o DICT.

        O histórico é contado pelo documento armazenado na conta, com as
        janelas do policy.py: o repository conta, o motor decide. E é contado
        DEPOIS da trava — o Pix do mesmo pagador que chegou junto, e
        passou na frente, já está gravado e entra na contagem, venha ele
        da conta que vier.
        """
        transaction_date = pix_request.transaction_date
        # Histórico e janelas usam exclusivamente o recebimento confiável do
        # servidor. transaction_date é o relógio de negócio do Pix e serve
        # para hora local, idade e auditoria; nunca pode mover o corte do
        # histórico para o futuro.
        history_time = received_at

        payer_history = self.pix_history_repository.get_payer_features(
            payer_account_id=payer.id,
            recipient_account_id=recipient.id,
            device_id=pix_request.device_id,
            ip=pix_request.ip,
            transaction_date=history_time,
            attempts_window=policy.PAYER_ATTEMPTS_WINDOW,
            amount_window=policy.PAYER_AMOUNT_WINDOW,
            new_destinations_window=policy.NEW_DESTINATIONS_WINDOW,
        )
        location_distance_km = self.pix_history_repository.get_location_distance_km(
            payer_account_id=payer.id,
            transaction_date=history_time,
            latitude=pix_request.latitude,
            longitude=pix_request.longitude,
        )
        device_history = self.pix_history_repository.get_device_features(
            device_id=pix_request.device_id,
            payer_account_id=payer.id,
            transaction_date=history_time,
            payers_window=policy.DEVICE_PAYERS_WINDOW,
        )
        recipient_history = self.pix_history_repository.get_recipient_features(
            recipient_account_id=recipient.id,
            payer_account_id=payer.id,
            transaction_date=history_time,
            attempts_window=policy.RECIPIENT_ATTEMPTS_WINDOW,
            payers_window=policy.RECIPIENT_PAYERS_WINDOW,
            attempts_cap=policy.HIGH_SUFFICIENCY_MIN_ATTEMPTS,
            days_cap=policy.HIGH_SUFFICIENCY_MIN_DAYS,
        )
        pair_history = self.pix_history_repository.get_pair_features(
            payer_account_id=payer.id,
            recipient_account_id=recipient.id,
            transaction_date=history_time,
            anomaly_window=policy.PAIR_ANOMALY_WINDOW,
        )
        try:
            device_trusted = self.device_registry_repository.is_trusted(
                account_id=payer.id,
                device_id=pix_request.device_id,
                at=history_time,
            )
            unregistered_device_day_amount = self.device_registry_repository.unregistered_device_day_amount(
                account_id=payer.id,
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
            payer_account_id=payer.id,
            recipient_account_id=recipient.id,
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
        payer_account_id: int,
        recipient_account_id: int,
        transaction_date: datetime,
        captured_at: datetime,
    ) -> dict:
        """Lê DICT security evidence uma vez e congela flags para o motor."""
        try:
            restriction = self.risk_repository.active_restriction(
                payer_account_id=payer_account_id,
                recipient_account_id=recipient_account_id,
                at=transaction_date,
            )
            payer_assessment = self.risk_repository.dict_security_evidence(
                account_id=payer_account_id, risk_subject="payer", at=transaction_date,
            )
            recipient_assessment = self.risk_repository.dict_security_evidence(
                account_id=recipient_account_id, risk_subject="recipient", at=transaction_date,
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
                "risk_subject": None,
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
                    "risk_subject": assessment.risk_subject,
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
        conta: o P01 olha "só a data de abertura informada". Conta
        aberta depois do Pix é dado inconsistente, e vira 0 — tratar como
        conta nova é o lado conservador, sem inventar um 400 que o contrato não
        prevê.
        """
        return max((transaction_date - account_data.opening_date).days, 0)

    def _create_pix_transaction(
        self, pix_request: PixRequest, analysis: Analysis, source_account: Account, destination_account: Account,
        *, received_at: datetime,
    ) -> PixTransaction | None:
        return self.pix_transaction_repository.create(
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
        )

    def _signal_rows(self, analysis: Analysis) -> list[dict]:
        """Sinais compactados no metadata da análise.

        Vão todos, inclusive os not_triggered e os unavailable: o GET
        mostra cada regra e o que ela disse, até quando foi "não sei".
        """
        rows = []
        for signal in analysis.signals:
            row = {
                "code": signal.code,
                "layer": signal_spec(signal.code).layer,
                "granularity": signal_spec(signal.code).granularity,
                "risk_subject": signal.axis,
                "family": signal.family,
                "points": signal.points,
                "status": signal.status,
            }
            if signal.evidence:
                row["evidence"] = signal.evidence
            rows.append(row)
        return rows

    def _recorded_analysis(self, pix_transaction: PixTransaction) -> dict:
        if pix_transaction.analysis is None:
            raise PixTransactionIdConflict(pix_transaction.external_id)
        return PixTransactionDTO.obj_to_dict(pix_transaction, pix_transaction.analysis)
