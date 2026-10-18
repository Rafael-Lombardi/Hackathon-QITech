"""Garantias de precedência e ownership que dependem da infraestrutura real."""

from datetime import timedelta, timezone
from uuid import uuid4

from sqlalchemy.exc import SQLAlchemyError

from controllers.pix_transaction_controller import PixTransactionController
from database import Context, clear_context, open_context
from repositories.device_registry_repository import DeviceRegistryRepository
from repositories.pix_transaction_repository import PixTransactionRepository
from tests.utils.clock import T0
from tests.utils.identities import new_payer, new_recipient
from tests.utils.pix import get_pix, pix_payload, post_pix
from tests.utils.risk_sources import seed_restrictions


def test_restricao_ativa_precede_fallback_de_cadastro_indisponivel(monkeypatch):
    """Restrição real continua rejeitando quando leitura de devices falha."""
    payer, recipient = new_payer(), new_recipient()
    payload = pix_payload(payer, recipient, T0)
    seed_restrictions([{
        "scope": "payer",
        "payer_document": payer.document,
        "active_from": T0.replace(hour=0),
        "occurrence_id": f"restriction-device-outage-{payer.document}",
    }])

    def unavailable(*args, **kwargs):
        raise SQLAlchemyError("device registry unavailable")

    monkeypatch.setattr(DeviceRegistryRepository, "is_trusted", unavailable)
    monkeypatch.setattr(DeviceRegistryRepository, "unregistered_device_day_amount", unavailable)

    context = open_context()
    try:
        controller = PixTransactionController()
        body, created = controller.create(payload, test_received_at=T0.astimezone(timezone.utc))
    finally:
        if context.db_session is not None:
            context.db_session.close()
        clear_context()

    assert created is True
    assert body["status"] == "rejected"
    assert body["metadata"]["decision_rule"] == "D-RESTRICT-SIM"


def test_owner_expirado_nao_conclui_receipt_reclamado_por_outro_owner():
    """Duas sessões reais: owner B assume após lease e owner A não sobrescreve."""
    payer, recipient = new_payer(), new_recipient()
    transaction_payload = pix_payload(payer, recipient, T0)
    transaction = post_pix(transaction_payload)
    assert transaction.http_status == 201

    lookup = Context()
    lookup.get_or_create_session()
    lookup_repository = PixTransactionRepository(lookup)
    persisted_transaction = lookup_repository.get_by_external_id(transaction_payload["id"])
    assert persisted_transaction is not None
    transaction_id = persisted_transaction.id
    lookup.db_session.close()

    received_at = T0.astimezone(timezone.utc)
    expired_at = received_at + timedelta(minutes=3)
    receipt_payload = {"id": f"lease-{uuid4()}"}

    first = Context()
    first.get_or_create_session()
    first_repository = PixTransactionRepository(first)
    first_receipt, first_claimed = first_repository.claim_receipt(
        external_id=receipt_payload["id"], payload=receipt_payload, now=received_at,
    )
    assert first_claimed is True
    first_token = first_receipt.owner_token
    first.db_session.commit()

    second = Context()
    second.get_or_create_session()
    second_repository = PixTransactionRepository(second)
    second_receipt, second_claimed = second_repository.claim_receipt(
        external_id=receipt_payload["id"], payload=receipt_payload, now=expired_at,
    )
    assert second_claimed is True
    second_token = second_receipt.owner_token
    second.db_session.commit()

    try:
        assert first_repository.mark_receipt_completed(
            first_receipt,
            owner_token=first_token,
            transaction_id=transaction_id,
            now=expired_at,
        ) is False
        first.db_session.rollback()

        assert second_repository.mark_receipt_completed(
            second_receipt,
            owner_token=second_token,
            transaction_id=transaction_id,
            now=expired_at,
        ) is True
        second.db_session.commit()
    finally:
        first.db_session.close()
        second.db_session.close()

    verification = Context()
    verification.get_or_create_session()
    try:
        persisted_receipt = PixTransactionRepository(verification).get_receipt(receipt_payload["id"])
        assert persisted_receipt is not None
        assert persisted_receipt.state == "completed"
        assert persisted_receipt.owner_token == second_token
    finally:
        verification.db_session.close()


def test_get_distingue_receipt_em_processamento_e_com_falha():
    now = T0.astimezone(timezone.utc)
    processing_id = f"processing-{uuid4()}"
    failed_id = f"failed-{uuid4()}"
    context = Context()
    context.get_or_create_session()
    repository = PixTransactionRepository(context)
    try:
        repository.claim_receipt(external_id=processing_id, payload={"id": processing_id}, now=now)
        failed, _ = repository.claim_receipt(external_id=failed_id, payload={"id": failed_id}, now=now)
        context.db_session.commit()
        repository.mark_receipt_failed(failed, owner_token=failed.owner_token, reason="simulated")
        context.db_session.commit()
    finally:
        context.db_session.close()

    processing = get_pix(processing_id)
    failed_response = get_pix(failed_id)
    assert processing.http_status == 409
    assert processing.body["code"] == "QIT001004"
    assert failed_response.http_status == 503
    assert failed_response.body["code"] == "QIT001005"
