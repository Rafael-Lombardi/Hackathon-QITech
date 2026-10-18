"""Smoke test do formato declarativo de cenários.

Este caso é pequeno de propósito: prova o runner e documenta o contrato de
armazenamento sem duplicar a bateria de cenários da RFC.
"""

from tests.utils.clock import T0
from tests.utils.identities import new_payer, new_recipient
from tests.utils.pix import pix_payload
from tests.utils.scenario import ExpectedAnalysis, Scenario


def test_scenario_declarativo_confere_post_e_get():
    payer, recipient = new_payer(), new_recipient()
    payload = pix_payload(payer, recipient, T0)

    unavailable = {code: "unavailable" for code in (
        "G01", "P04", "P05", "P06", "P07", "P08", "P09", "P10",
        "R02", "R03", "R04", "REL01", "REL02", "DICT02", "DICT03",
    )}
    not_triggered = {code: "not_triggered" for code in ("P01", "L01", "R01", "REL03", "L02", "DICT01")}

    Scenario(
        name="primeira-observacao",
        authoritative={"device_registry": "empty", "dict": "default"},
        derived={"baseline": "unavailable"},
        request=payload,
        expected=ExpectedAnalysis(
            status="approved",
            decision_rule="D-OK",
            scores={"payer": 18, "recipient": 0},
            sufficiency={"payer": "unavailable", "recipient": "unavailable"},
            signals={**not_triggered, **unavailable, "P03": "triggered"},
            points={"P01": 0, "L01": 0, "R01": 0, "REL03": 0, "P03": 18, "DICT01": 0},
        ),
    ).run()


def test_scenario_device_cadastrado_nao_e_observado():
    payer, recipient = new_payer(), new_recipient()
    payload = pix_payload(payer, recipient, T0)
    unavailable = {code: "unavailable" for code in (
        "G01", "P04", "P05", "P06", "P07", "P08", "P09", "P10",
        "R02", "R03", "R04", "REL01", "REL02", "DICT02", "DICT03",
    )}
    not_triggered = {code: "not_triggered" for code in (
        "P01", "L01", "R01", "REL03", "P03", "L02", "DICT01",
    )}

    Scenario(
        name="dispositivo-cadastrado-primeira-observacao",
        authoritative={"devices": [{
            "payer_document": payer.document,
            "device_id": payer.device_id,
            "registered_at": "2026-10-01T00:00:00+00:00",
        }]},
        request=payload,
        expected=ExpectedAnalysis(
            status="approved",
            decision_rule="D-OK",
            scores={"payer": 0, "recipient": 0},
            sufficiency={"payer": "unavailable", "recipient": "unavailable"},
            signals={**not_triggered, **unavailable},
            points={"P03": 0},
        ),
    ).run()
