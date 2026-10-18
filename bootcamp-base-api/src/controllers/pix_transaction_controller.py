from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from antifraud import Analysis, Context, DeviceRegistration, Restriction, Transaction, evaluate, policy
from connectors import DeviceRegistryConnector, DictConnector, RestrictionsConnector
from connectors.device_registry_connector import DeviceRegistryRecord
from connectors.dict_connector import DictSecurityInformation
from controllers.base_controller import BaseController
from controllers.pix_request import AccountData, PixRequest, parse_pix_request
from dtos import PixTransactionDTO
from errors import (
    DictUnavailable,
    PixTransactionEvaluationFailed,
    PixTransactionIdConflict,
    PixTransactionNotFound,
    PixTransactionProcessing,
    QIException,
    TransactionDateOutOfTolerance,
)
from models import Account, Customer, PixTransaction, ReceiptState
from repositories import AccountRepository, CustomerRepository, PixHistoryRepository, PixTransactionRepository
from utils.document_number import mask_document

# Por quanto tempo um recibo em processing é de quem o reivindicou. Mais
# que a pior avaliação possível (três fontes a 2 s cada, mais os 10 s de
# teto de um comando no banco) e pouco o bastante pra que o id de um
# avaliador que morreu volte a ser avaliável logo (RFC §8).
RECEIPT_LEASE = timedelta(minutes=2)

# O Retry-After do 409 "em processamento": a avaliação leva milissegundos
# quando as fontes respondem, e segundos quando não.
PROCESSING_RETRY_AFTER_SECONDS = 1


@dataclass(frozen=True)
class _Claim:
    """A reivindicação de um recibo: o que o avaliador precisa guardar depois do commit dela.

    São valores soltos, e não o model: depois do commit o SQLAlchemy
    expira o objeto, e ler um atributo dele abriria outra transação — no
    meio das consultas às fontes, com a conexão presa.
    """

    receipt_id: int
    owner_token: str
    received_at: datetime


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

    def create(self, payload: dict, *, test_received_at: datetime | None = None) -> tuple[dict, bool]:
        """Analisa um Pix novo, ou devolve a análise que ele já tem.

        Devolve o corpo da resposta e se a análise nasceu agora: o
        resource transforma isso em 201 ou 200. test_received_at é o
        relógio de teste (controllers/pix_request.py); None é o relógio
        de verdade.

        A ordem é a regra, e cada passo tem um porquê:

          1. converte o payload — data impossível é 400, antes de tudo — e
             confere a tolerância de transaction_date contra o relógio:
             data de meses atrás, ou de amanhã, é 400, e não veredito;
          2. reivindica o recibo do id, numa transação curta só dele, e
             commita: é a prova de que o Pix chegou, e sobrevive a tudo
             que vier depois. É aqui que o reenvio é resolvido — mesmo
             conteúdo já avaliado devolve a análise gravada, conteúdo
             diferente é 409, avaliação em curso é 409 "em processamento",
             sem esperar e sem segundo avaliador;
          3. consulta as três fontes ANTES de travar qualquer coisa: as
             restrições primeiro, porque sem elas não há veredito (503),
             depois o cadastro de aparelhos e o DICT. Cada consulta pode
             levar os 2 s do prazo, e um pagador travado esse tempo todo
             seguraria todos os Pix dele;
          4. trava o pagador — pelo documento, porque as regras do
             pagador contam pelo documento, somando as contas dele — e só
             então cria customers e contas na primeira aparição;
          5. lê o relógio da avaliação (evaluated_at), já com a trava na
             mão: todo Pix do pagador commitado antes tem received_at até
             aqui, e nenhum escapa da janela;
          6. conta o histórico, lê o que as fontes responderam nesse
             instante, monta o Context e roda o motor;
          7. grava o Pix e os sinais e conclui o recibo na mesma
             transação, com a cerca do owner_token: um avaliador que
             perdeu o lease desfaz tudo em vez de gravar por cima.

        Qualquer falha depois do recibo o deixa failed, com o motivo, e o
        erro segue (500, ou o 503 das restrições): o GET diz que o Pix
        chegou e não foi avaliado, e o reenvio avalia de novo.
        """
        pix_request = parse_pix_request(payload)
        received_at = test_received_at or datetime.now(timezone.utc)
        self._check_transaction_date_tolerance(pix_request, received_at)

        claim = self._claim_receipt(pix_request, received_at)
        if claim is None:
            return self._recorded_analysis(self._recorded_pix(pix_request.external_id)), False

        try:
            return self._evaluate(pix_request, claim, test_received_at)
        except Exception as error:
            self.session.rollback()
            self.pix_transaction_repository.fail_receipt(
                claim.receipt_id, owner_token=claim.owner_token, reason=_failure_reason(error),
            )
            self.session.commit()
            raise

    def _evaluate(self, pix_request: PixRequest, claim: _Claim, test_received_at: datetime | None) -> tuple[dict, bool]:
        # O rollback não desfaz nada — o recibo já foi commitado. Ele
        # devolve a conexão ao pool, o estoque de conexões de banco que o
        # processo divide entre todas as requisições, pra outra usar
        # enquanto este Pix espera as fontes: com uma fonte lenta, segurar
        # a conexão na espera derrubava a vazão pela metade.
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

        # O relógio da avaliação, lido DEPOIS da trava: o Pix do mesmo
        # pagador que passou na frente já está commitado, com received_at
        # até este instante, e entra nas janelas. No relógio de teste, é o
        # mesmo instante do recibo.
        evaluated_at = test_received_at or datetime.now(timezone.utc)
        context = self._build_context(
            pix_request, payer, recipient, registry_record, restrictions, dict_information, evaluated_at,
        )
        analysis = evaluate(context)

        pix_transaction = self._create_pix_transaction(
            pix_request, analysis, context, source_account, destination_account,
            received_at=claim.received_at, evaluated_at=evaluated_at,
        )
        completed = pix_transaction is not None and self.pix_transaction_repository.complete_receipt(
            claim.receipt_id, owner_token=claim.owner_token, pix_transaction_id=pix_transaction.id,
            evaluated_at=evaluated_at,
        )
        if not completed:
            # Perdemos a posse do recibo: o lease venceu e outro avaliador
            # reivindicou o id (ou já gravou a análise dele). O que fizemos
            # não vale, e a resposta é a do estado atual do recibo.
            self.session.rollback()
            return self._answer_from_receipt(pix_request.external_id), False

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
            return self._answer_from_receipt(external_id)

        return self._recorded_analysis(pix_transaction)

    def _check_transaction_date_tolerance(self, pix_request: PixRequest, received_at: datetime) -> None:
        """400 se transaction_date está longe demais do relógio: os limites estão no policy.py."""
        earliest = received_at - policy.TRANSACTION_DATE_MAX_AGE
        latest = received_at + policy.TRANSACTION_DATE_MAX_SKEW
        if not earliest <= pix_request.transaction_date <= latest:
            raise TransactionDateOutOfTolerance(
                pix_request.payload["transaction_date"],
                policy.TRANSACTION_DATE_MAX_AGE.days,
                policy.TRANSACTION_DATE_MAX_SKEW.seconds // 60,
            )

    def _claim_receipt(self, pix_request: PixRequest, received_at: datetime) -> _Claim | None:
        """Reivindica o recibo do id e commita; None quando a análise já existe e é o caso de devolvê-la.

        O INSERT ganha se o id é inédito. Se não é, o recibo é lido com a
        linha travada, e o estado dele decide (RFC §8): conteúdo diferente
        é 409 de conflito, seja qual for o estado; completed é o reenvio;
        processing com lease vigente é 409 "em processamento", sem esperar
        — não se inicia um segundo avaliador; failed, ou processing com
        lease vencido, é reivindicado por compare-and-swap. Quem perde o
        CAS perdeu pra outro reenvio que chegou junto: também "em
        processamento".

        O commit no fim é o que torna o recibo durável antes de qualquer
        fonte ou trava; o rollback dos outros caminhos só solta a linha.
        """
        payload_hash = pix_request.canonical_hash()
        owner_token = str(uuid4())
        lease_until = received_at + RECEIPT_LEASE

        receipt = self.pix_transaction_repository.create_receipt(
            external_id=pix_request.external_id, payload_hash=payload_hash, received_at=received_at,
            owner_token=owner_token, lease_until=lease_until,
        )
        if receipt is None:
            receipt = self.pix_transaction_repository.get_receipt(pix_request.external_id, for_update=True)
            if receipt.payload_hash != payload_hash:
                self.session.rollback()
                raise PixTransactionIdConflict(pix_request.external_id)
            if receipt.state == ReceiptState.COMPLETED:
                self.session.rollback()
                self.logger.info(f"Pix {pix_request.external_id} reenviado: devolvendo a análise gravada")
                return None
            if receipt.state == ReceiptState.PROCESSING and receipt.lease_until > received_at:
                self.session.rollback()
                raise PixTransactionProcessing(pix_request.external_id, PROCESSING_RETRY_AFTER_SECONDS)
            reclaimed = self.pix_transaction_repository.reclaim_receipt(
                receipt, owner_token=owner_token, received_at=received_at, lease_until=lease_until,
            )
            if not reclaimed:
                self.session.rollback()
                raise PixTransactionProcessing(pix_request.external_id, PROCESSING_RETRY_AFTER_SECONDS)
            self.logger.info(f"Pix {pix_request.external_id} reivindicado de novo: o recibo estava {receipt.state}")

        claim = _Claim(receipt_id=receipt.id, owner_token=owner_token, received_at=received_at)
        self.session.commit()
        return claim

    def _answer_from_receipt(self, external_id: str) -> dict:
        """A resposta de um id que não tem análise gravada: o recibo diz qual.

        completed devolve a análise (um avaliador acabou de gravar);
        processing é 409 "em processamento"; failed é 503 — o Pix chegou,
        a avaliação falhou, e o reenvio avalia de novo; sem recibo, 404.
        Nunca sucesso forjado (RFC §8).
        """
        self.session.rollback()
        receipt = self.pix_transaction_repository.get_receipt(external_id)
        if receipt is None:
            raise PixTransactionNotFound(external_id)
        if receipt.state == ReceiptState.COMPLETED:
            return self._recorded_analysis(self._recorded_pix(external_id))
        if receipt.state == ReceiptState.FAILED:
            raise PixTransactionEvaluationFailed(external_id)
        raise PixTransactionProcessing(external_id, PROCESSING_RETRY_AFTER_SECONDS)

    def _recorded_pix(self, external_id: str) -> PixTransaction:
        # Recibo completed sem análise não existe (o CHECK da tabela amarra
        # os dois): se o SELECT voltar vazio, é defeito nosso, e tem de
        # aparecer alto, como 500.
        pix_transaction = self.pix_transaction_repository.get_by_external_id(external_id=external_id)
        if pix_transaction is None:
            raise RuntimeError(f"recibo completed sem análise gravada para o id {external_id}")
        return pix_transaction

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
        evaluated_at: datetime,
    ) -> Context:
        """O Context do motor: o Pix normalizado, o histórico contado e o que as fontes disseram.

        O histórico é contado por documento (o customer), com as janelas
        do policy.py e o relógio do servidor — received_at dos Pix
        gravados, até o evaluated_at deste (o porquê está no
        pix_history_repository.py): o repository conta, o motor decide. E
        é contado DEPOIS da trava — o Pix do mesmo pagador que chegou
        junto, e passou na frente, já está gravado e entra na contagem,
        venha ele da conta que vier. As contas dos dois documentos vão
        prontas pras consultas do pagador e do par; o porquê está no
        AccountRepository.ids_of.

        As fontes foram consultadas antes da trava, e o que elas
        responderam é lido AQUI, no instante da avaliação: o cadastro do
        aparelho tem vigência, a restrição também, e as duas se resolvem
        contra o mesmo relógio do histórico. transaction_date, a data do
        payload, fica com o que é do negócio: a hora local do L01 e a
        idade das contas.
        """
        transaction_date = pix_request.transaction_date
        accounts = self.account_repository.ids_of(customer_ids={payer.id, recipient.id})

        payer_history = self.pix_history_repository.get_payer_features(
            payer_accounts=accounts[payer.id],
            recipient_customer_id=recipient.id,
            device_id=pix_request.device_id,
            ip=pix_request.ip,
            at=evaluated_at,
            attempts_window=policy.PAYER_ATTEMPTS_WINDOW,
            amount_window=policy.PAYER_AMOUNT_WINDOW,
            new_destinations_window=policy.NEW_DESTINATIONS_WINDOW,
        )
        location_distance_km = self.pix_history_repository.get_location_distance_km(
            payer_customer_id=payer.id,
            at=evaluated_at,
            latitude=pix_request.latitude,
            longitude=pix_request.longitude,
        )
        device_history = self.pix_history_repository.get_device_features(
            device_id=pix_request.device_id,
            payer_customer_id=payer.id,
            at=evaluated_at,
            payers_window=policy.DEVICE_PAYERS_WINDOW,
        )
        recipient_history = self.pix_history_repository.get_recipient_features(
            recipient_customer_id=recipient.id,
            payer_customer_id=payer.id,
            at=evaluated_at,
            attempts_window=policy.RECIPIENT_ATTEMPTS_WINDOW,
            payers_window=policy.RECIPIENT_PAYERS_WINDOW,
            attempts_cap=policy.HIGH_SUFFICIENCY_MIN_ATTEMPTS,
            days_cap=policy.HIGH_SUFFICIENCY_MIN_DAYS,
        )
        pair_history = self.pix_history_repository.get_pair_features(
            payer_accounts=accounts[payer.id],
            recipient_accounts=accounts[recipient.id],
            at=evaluated_at,
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
            status=registry_record.status_at(evaluated_at),
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
            ) and restriction.active_at(evaluated_at)
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
        source_account: Account, destination_account: Account, *, received_at: datetime, evaluated_at: datetime,
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
            evaluated_at=evaluated_at,
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


def _failure_reason(error: Exception) -> str:
    """O motivo que vai pro recibo failed: nome da classe e, se o erro é nosso, a descrição dele.

    Só a descrição dos erros nossos, que são escritas sem documento. A
    mensagem de uma exceção qualquer pode trazer o que não vai pro banco
    nem pro log — a URL com o CPF, um pedaço do payload —, e dela fica só
    o nome; o traceback inteiro já está no log.
    """
    if isinstance(error, QIException):
        return f"{type(error).__name__} ({error.code}): {error.description}"
    return type(error).__name__


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
