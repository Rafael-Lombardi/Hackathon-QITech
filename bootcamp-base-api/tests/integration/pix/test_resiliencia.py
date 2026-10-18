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
from tests.utils.pix import pix_payload, post_pix
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
    lookup_repository = PixTransactionRepository(lookup)
    persisted_transaction = lookup_repository.get_by_external_id(transaction_payload["id"])
    assert persisted_transaction is not None
    transaction_id = persisted_transaction.id
    lookup.db_session.close()

    received_at = T0.astimezone(timezone.utc)
    expired_at = received_at + timedelta(minutes=3)
    receipt_payload = {"id": f"lease-{uuid4()}"}

    first = Context()
    first_repository = PixTransactionRepository(first)
    first_receipt, first_claimed = first_repository.claim_receipt(
        external_id=receipt_payload["id"], payload=receipt_payload, now=received_at,
    )
    assert first_claimed is True
    first_token = first_receipt.owner_token
    first.db_session.commit()

    second = Context()
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
    try:
        persisted_receipt = PixTransactionRepository(verification).get_receipt(receipt_payload["id"])
        assert persisted_receipt is not None
        assert persisted_receipt.state == "completed"
        assert persisted_receipt.owner_token == second_token
    finally:
        verification.db_session.close()
