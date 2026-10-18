"""Cenários de rejeição demonstrativa introduzidos pela RFC v0.5."""

from datetime import timedelta

import pytest

from tests.utils.clock import T0, days_before, minutes_before
from tests.utils.dict_mock import DictMock
from tests.utils.identities import new_device, new_payer, new_recipient
from tests.utils.pix import pix_payload, post_pix, seed, seed_pix
from tests.utils.device_registry import seed_devices
from tests.utils.risk_sources import seed_dict_security_evidence, seed_restrictions


def _recipient_network_with_external(assessment: str, dict_mock: DictMock):
    recipient = new_recipient()
    payers = [new_payer() for _ in range(4)]
    seed([
        pix_payload(payer, recipient, moment)
        for payer, moment in zip(payers, minutes_before(T0, 4))
    ])
    dict_mock.set_trust(recipient.document, 12)
    seed_dict_security_evidence([{
        "customer_document": recipient.document,
        "axis": "recipient",
        "assessment": assessment,
        "valid_from": T0.replace(hour=0),
        "occurrence_id": f"external-{assessment}-{recipient.document}",
    }])
    return recipient


class TestPoliticaRfcV05:
    def test_rejeicao_ato_exige_combinacao_completa(self):
        """D-REJ-ATO — tomada de conta com sinais completos.

        Preparo: 10 aprovações de R$ 1.000, distribuídas em 10 dias, para
        qualificar P06. Depois, quatro tentativas de R$ 6.000 em quatro
        minutos, usando um device sem registro e quatro destinos inéditos.
        O Pix atual repete o padrão.

            P03 +18   device não registrado
            P04 +20   quinta tentativa em 5 min
            P06 +18   valor 6x a mediana
            P07 +18   cinco destinos inéditos em 10 min
            L02 +10   valor acima do limite do device não registrado
            payer >= 70 após os tetos

        A combinação completa rejeita antes de qualquer regra de 2FA.
        """
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
        # O L02 também dispara (> R$ 200 em device não registrado), então
        # o score chega a 80; o limiar da rejeição continua sendo 70.
        assert response.scores["payer"] >= 70

    def test_ato_incompleto_fica_em_2fa(self):
        """ATO incompleto — ausência de P07 impede rejeição definitiva.

        O histórico mantém a rajada e o valor atípico, mas todas as
        tentativas usam o mesmo destinatário. Assim P03 e P06 disparam,
        enquanto P07 permanece `not_triggered`.

        O resultado é D-ATO-1 (2FA), e não D-REJ-ATO: a política não trata
        duas evidências de tomada de conta como prova suficiente para
        rejeição.
        """
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
        assert response.decision_rule == "D-ATO-1"
        assert response.signal_status("P07") == "not_triggered"

    def test_rejeicao_payer_exige_inteligencia_do_pagador(self):
        """D-REJ-PAY — comportamento extremo mais inteligência externa.

        Preparo: 10 aprovações qualificam a mediana; o device do pagador é
        registrado e também aparece para três outros pagadores (P08). Em
        seguida há quatro tentativas para destinos inéditos; três são de
        R$ 1.000 e a quarta de R$ 6.000, evitando 2FA antes da rajada final.
        A fixture externa marca o pagador como `suspicious`.

            P04 + P05   velocidade/montante, teto 25
            P06 + P07 + P08  comportamento, teto 45
            payer = 70 ou mais
            external payer = suspicious

        Sem a avaliação externa, a mesma combinação fica em 2FA; com ela,
        D-REJ-PAY rejeita.
        """
        payer, usual = new_payer(), new_recipient()
        seed_pix(payer, usual, days_before(T0, 10), amount=100000)
        shared_device = new_device()
        seed_devices([{
            "payer_document": payer.document,
            "device_id": shared_device,
            "registered_at": T0.replace(hour=0),
        }])
        other_payers = [new_payer() for _ in range(3)]
        # A janela do P08 é estritamente maior que ``T0 - 24h``.  Usar
        # exatamente um dia antes cai na borda aberta da janela e faz os
        # três pagadores desaparecerem da contagem, deixando o score em
        # 2FA (sem os 12 pontos do dispositivo compartilhado).
        shared_device_moment = T0 - timedelta(hours=23)
        seed([
            pix_payload(other, usual, shared_device_moment, device_id=shared_device)
            for other in other_payers
        ])
        targets = [new_recipient() for _ in range(4)]
        seed(
            [
                pix_payload(payer, target, moment, amount=amount, device_id=shared_device)
                for target, moment, amount in zip(
                    targets, minutes_before(T0, 4), (100000, 100000, 100000, 600000)
                )
            ],
            expect=["approved", "approved", "approved", "two_factor_required"],
        )
        seed_dict_security_evidence([{
            "customer_document": payer.document,
            "axis": "payer",
            "assessment": "suspicious",
            "valid_from": T0.replace(hour=0),
            "occurrence_id": f"external-payer-{payer.document}",
        }])

        response = post_pix(pix_payload(payer, new_recipient(), T0, amount=600000, device_id=shared_device))

        assert response.status == "rejected"
        assert response.decision_rule == "D-REJ-PAY"
        assert response.scores["payer"] >= 70

    def test_restricao_ativa_tem_precedencia(self):
        """Restrição ativa — fonte autoritativa vence qualquer score.

        Uma restrição `payer` válida no instante T0 é seedada antes do POST.
        O Pix é normal, mas a política retorna D-RESTRICT-SIM antes de
        consultar o fallback de device ou as regras de risco.
        """
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
        """Restrição fora da janela de validade não é restrição ativa.

        O mesmo cenário roda com uma restrição futura e com uma restrição já
        expirada. Em ambos os casos a consulta autoritativa encontra o
        registro, mas não o considera vigente; um Pix normal permanece
        D-OK.
        """
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
        """D-REJ-RCP — rede de recebimento suspeita com evidência externa.

        Quatro pagadores diferentes fazem entradas no mesmo titular em
        quatro minutos; o Pix atual completa R02 e R03. O DICT retorna 12,
        acionando DICT01 (+32), e a fixture externa marca o titular como
        `suspicious`.

            R02 +16 + R03 +20 = 36, limitado a 35 (rede)
            DICT01 +32, teto separado = 32
            recipient = 87 (DICT01 32 + rede 35 + DICT02 20)

        O ponto essencial é a evidência externa: sem ela, a mesma rede fica
        em D-RCP/2FA; com ela e recipient >=55, D-REJ-RCP rejeita.
        """
        recipient = _recipient_network_with_external("suspicious", dict_mock)

        response = post_pix(pix_payload(new_payer(), recipient, T0))

        assert response.status == "rejected"
        assert response.decision_rule == "D-REJ-RCP"
        assert response.scores["recipient"] == 87
        assert response.signal_status("DICT02") == "triggered"

    @pytest.mark.parametrize("assessment", ["clear", "unavailable"])
    def test_inteligencia_clear_ou_indisponivel_nao_rejeita(self, dict_mock: DictMock, assessment):
        """Inteligência `clear` ou `unavailable` nunca vira rejeição.

        O histórico, o DICT e os sinais internos são idênticos ao caso
        D-REJ-RCP. Apenas a avaliação externa muda para `clear` ou
        `unavailable`. A ausência de evidência adversa mantém D-RCP/2FA;
        ela não é interpretada como confirmação de segurança nem de fraude.
        """
        recipient = _recipient_network_with_external(assessment, dict_mock)

        response = post_pix(pix_payload(new_payer(), recipient, T0))

        assert response.status == "two_factor_required"
        assert response.decision_rule == "D-RCP"
        assert response.scores["recipient"] == 67
        expected_signal_status = "not_triggered" if assessment == "clear" else "unavailable"
        assert response.signal_status("DICT02") == expected_signal_status

    def test_rejeicao_cross_combina_os_dois_eixos(self, dict_mock: DictMock):
        """D-REJ-CROSS — risco relevante nos dois eixos.

        O pagador acumula P04, P05 e P07 até ultrapassar 40. O destino é
        novo para um pagador já conhecido (REL01), recebe DICT01 baixo e a
        inteligência externa retorna `confirmed_in_mock`.

            payer >= 40
            recipient >= 40
            external recipient = confirmed_in_mock

        A política cruza os dois eixos e rejeita. Sem a evidência externa,
        o mesmo score seguiria para 2FA pela regra do pagador.
        """
        payer, old_recipient = new_payer(), new_recipient()
        seed_pix(payer, old_recipient, days_before(T0, 3))
        targets = [new_recipient() for _ in range(4)]
        seed([
            pix_payload(payer, target, moment, amount=200000)
            for target, moment in zip(targets, minutes_before(T0, 4))
        ])
        recipient = new_recipient()
        dict_mock.set_trust(recipient.document, 12)
        seed_dict_security_evidence([{
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
        assert response.signal_status("DICT03") == "triggered"
