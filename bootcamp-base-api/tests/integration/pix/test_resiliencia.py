"""Garantias de precedência e fallback que dependem da infraestrutura real."""

from datetime import timezone

from sqlalchemy.exc import SQLAlchemyError

from controllers.pix_transaction_controller import PixTransactionController
from database import Context, clear_context, open_context
from repositories.device_registry_repository import DeviceRegistryRepository
from tests.utils.clock import T0
from tests.utils.identities import new_payer, new_recipient
from tests.utils.pix import pix_payload
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
