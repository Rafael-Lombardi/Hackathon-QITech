"""Cenários de rejeição demonstrativa introduzidos pela RFC v0.5."""

from datetime import timedelta

import pytest

from tests.utils.clock import T0, days_before, minutes_before
from tests.utils.dict_mock import DictMock
from tests.utils.identities import new_device, new_payer, new_recipient
from tests.utils.pix import pix_payload, post_pix, seed, seed_pix
from tests.utils.device_registry import seed_devices
from tests.utils.risk_sources import seed_external_risk, seed_restrictions


def _recipient_network_with_external(assessment: str, dict_mock: DictMock):
    recipient = new_recipient()
    payers = [new_payer() for _ in range(4)]
    seed([
        pix_payload(payer, recipient, moment)
        for payer, moment in zip(payers, minutes_before(T0, 4))
    ])
    dict_mock.set_trust(recipient.document, 12)
    seed_external_risk([{
        "customer_document": recipient.document,
        "axis": "recipient",
        "assessment": assessment,
        "valid_from": T0.replace(hour=0),
        "occurrence_id": f"external-{assessment}-{recipient.document}",
    }])
    return recipient


class TestPoliticaRfcV05:
    def test_rejeicao_ato_exige_combinacao_completa(self):
        payer, usual = new_payer(), new_recipient()
        seed_pix(payer, usual, days_before(T0, 10), amount=100000)
        device_id = new_device()
        targets = [new_recipient() for _ in range(4)]
        seed(
            [
                pix_payload(payer, target, moment, amount=600000, device_id=device_id)
                for target, moment in zip(targets, minutes_before(T0, 4))
            ],
            expect="two_factor_required",
        )

        response = post_pix(pix_payload(payer, new_recipient(), T0, amount=600000, device_id=device_id))

        assert response.status == "rejected"
        assert response.decision_rule == "D-REJ-ATO"
        assert response.scores["payer"] == 70

    def test_ato_incompleto_fica_em_2fa(self):
        payer, recipient = new_payer(), new_recipient()
        seed_pix(payer, recipient, days_before(T0, 10), amount=100000)
        device_id = new_device()
        # Rajada e valor atípico, mas sempre para o mesmo destino: P07 não
        # dispara, logo a rejeição ATO não pode casar.
        seed(
            [pix_payload(payer, recipient, moment, amount=600000, device_id=device_id)
             for moment in minutes_before(T0, 4)],
            expect="two_factor_required",
        )
        response = post_pix(pix_payload(payer, recipient, T0, amount=600000, device_id=device_id))

        assert response.status == "two_factor_required"
        assert response.decision_rule == "D-PAY"
        assert response.signal_status("P07") == "not_triggered"

    def test_rejeicao_payer_exige_inteligencia_do_pagador(self):
        payer, usual = new_payer(), new_recipient()
        seed_pix(payer, usual, days_before(T0, 10), amount=100000)
        shared_device = new_device()
        seed_devices([{
            "payer_document": payer.document,
            "device_id": shared_device,
            "registered_at": T0.replace(hour=0),
        }])
        other_payers = [new_payer() for _ in range(3)]
        seed([
            pix_payload(other, usual, days_before(T0, 1)[0], device_id=shared_device)
            for other in other_payers
        ])
        targets = [new_recipient() for _ in range(4)]
        seed(
            [
                pix_payload(payer, target, moment, amount=600000, device_id=shared_device)
                for target, moment in zip(targets, minutes_before(T0, 4))
            ],
            expect="two_factor_required",
        )
        seed_external_risk([{
            "customer_document": payer.document,
            "axis": "payer",
            "assessment": "suspicious",
            "valid_from": T0.replace(hour=0),
            "occurrence_id": f"external-payer-{payer.document}",
        }])

        response = post_pix(pix_payload(payer, new_recipient(), T0, amount=600000, device_id=shared_device))

        assert response.status == "rejected"
        assert response.decision_rule == "D-REJ-PAY"
        assert response.scores["payer"] == 70

    def test_restricao_ativa_tem_precedencia(self):
        payer, recipient = new_payer(), new_recipient()
        payload = pix_payload(payer, recipient, T0)
        seed_restrictions([{
            "scope": "payer",
            "payer_document": payer.document,
            "active_from": T0.replace(hour=0),
            "occurrence_id": f"restriction-{payer.document}",
        }])

        response = post_pix(payload)

        assert response.status == "rejected"
        assert response.decision_rule == "D-RESTRICT-SIM"

    @pytest.mark.parametrize(
        ("active_from", "active_until"),
        [
            (T0 + timedelta(hours=1), None),
            (T0 - timedelta(days=2), T0 - timedelta(hours=1)),
        ],
        ids=["futura", "expirada"],
    )
    def test_restricao_futura_ou_expirada_nao_rejeita(self, active_from, active_until):
        payer, recipient = new_payer(), new_recipient()
        seed_restrictions([{
            "scope": "payer",
            "payer_document": payer.document,
            "active_from": active_from,
            "active_until": active_until,
            "occurrence_id": f"restriction-temporal-{payer.document}",
        }])

        response = post_pix(pix_payload(payer, recipient, T0))

        assert response.status == "approved"
        assert response.decision_rule == "D-OK"

    def test_rejeicao_de_recebedor_exige_inteligencia_externa(self, dict_mock: DictMock):
        recipient = _recipient_network_with_external("suspicious", dict_mock)

        response = post_pix(pix_payload(new_payer(), recipient, T0))

        assert response.status == "rejected"
        assert response.decision_rule == "D-REJ-RCP"
        assert response.scores["recipient"] == 67

    @pytest.mark.parametrize("assessment", ["clear", "unavailable"])
    def test_inteligencia_clear_ou_indisponivel_nao_rejeita(self, dict_mock: DictMock, assessment):
        recipient = _recipient_network_with_external(assessment, dict_mock)

        response = post_pix(pix_payload(new_payer(), recipient, T0))

        assert response.status == "two_factor_required"
        assert response.decision_rule == "D-RCP"
        assert response.scores["recipient"] == 67

    def test_rejeicao_cross_combina_os_dois_eixos(self, dict_mock: DictMock):
        payer, old_recipient = new_payer(), new_recipient()
        seed_pix(payer, old_recipient, days_before(T0, 3))
        targets = [new_recipient() for _ in range(4)]
        seed([
            pix_payload(payer, target, moment, amount=200000)
            for target, moment in zip(targets, minutes_before(T0, 4))
        ])
        recipient = new_recipient()
        dict_mock.set_trust(recipient.document, 12)
        seed_external_risk([{
            "customer_document": recipient.document,
            "axis": "recipient",
            "assessment": "confirmed_in_mock",
            "valid_from": T0.replace(hour=0),
            "occurrence_id": f"external-cross-{recipient.document}",
        }])

        response = post_pix(pix_payload(payer, recipient, T0, amount=200000))

        assert response.status == "rejected"
        assert response.decision_rule == "D-REJ-CROSS"
        assert response.scores["payer"] >= 40
        assert response.scores["recipient"] >= 40
