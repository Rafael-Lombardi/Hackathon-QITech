"""As rejeições: as quatro linhas da tabela que negam o Pix por nota, em vez de pedir 2FA.

Rejeitar é a decisão cara — "bloquear um cliente bom também é prejuízo"
—, e por isso a tabela só rejeita por nota com corroboração: a tomada de
conta com todos os sinais juntos (D-REJ-ATO), ou a mesma rajada com o
DICT apontando o próprio pagador (D-REJ-PAY), ou um destino que o DICT
aponta — evidência de fraude ou fraude confirmada — recebendo rajada de
muita gente (D-REJ-RCP) ou com os dois eixos no limiar (D-REJ-CROSS). A
evidência vem do mock do DICT, como ocorrência datada na mesma resposta
da estatística, e cada teste escreve a do pagador ou do recebedor dele
pela fixture sources_mock. O negativo de cada rejeição fica ao lado dela:
a mesma história sem a peça que rejeita — sem a ocorrência, com a
ocorrência vencida, ou só com uma denúncia não resolvida, que não abre
porteira nenhuma —, e o que sobra é 2FA. A outra rejeição, a restrição
ativa (D-RESTRICT-SIM), não olha nota nenhuma e tem arquivo próprio,
test_restricoes.py.
"""

from datetime import timedelta

import pytest

from tests.utils.clock import T0, at, days_before, minutes_before
from tests.utils.identities import Payer, Recipient, new_device, new_payer, new_recipient
from tests.utils.pix import pix_payload, post_pix, seed, seed_pix
from tests.utils.sources_mock import occurrence

# A ocorrência que abre a porteira (fraud_related_evidence ou
# confirmed_in_mock), a que não abre (unresolved_report), a vencida e
# nenhuma: os quatro lados de cada rejeição por evidência.
EVIDENCE = [occurrence("fraud_related_evidence")]
CONFIRMED = [occurrence("confirmed_in_mock")]
REPORT = [occurrence("unresolved_report")]
EXPIRED = [occurrence("fraud_related_evidence", reported_at=T0 - timedelta(days=30), expires_at=T0 - timedelta(hours=1))]


def _crowded_recipient() -> Recipient:
    """Um titular de conta antiga que 4 pagadores novos pagaram nos 4 minutos antes de T0.

    É a rede do R02 e do R03 pronta pro 5º Pix: 5 entradas em 10 min e 5
    pagadores em 1 h. As 4 sementes saem aprovadas — a 4ª já vê 4
    pagadores (R03 +20) e para em 20.
    """
    recipient = new_recipient()
    seed([pix_payload(new_payer(), recipient, moment) for moment in minutes_before(T0, 4)])
    return recipient


def _known_payer() -> Payer:
    """Um pagador com 1 Pix de ontem pra outro destino: o bastante pro REL01 responder."""
    payer = new_payer()
    seed_pix(payer, new_recipient(), [T0 - timedelta(days=1)])
    return payer


class TestRejeicoes:
    def test_rejeicao_ato_pede_os_quatro_sinais_e_70(self):
        """D-REJ-ATO — a tomada de conta com tudo junto: aparelho sem cadastro, rajada, valor atípico e destinos inéditos.

        Preparo: 10 Pix de R$ 1.000 pro destino de sempre, um por dia nos
        10 dias anteriores, às 9h (mediana R$ 1.000, com o lastro do P06 —
        baseline inteiro pra qualquer Pix de hoje à tarde). Nos 4 minutos
        antes de T0, 4 Pix de R$ 6.000 de um aparelho sem cadastro, cada um
        pra um destino inédito — os 4 terminam em 2FA pelo D-ATO-1. Pix: o
        5º, igual.

            P03 +18    aparelho sem cadastro no banco
            P06 +18    R$ 6.000 é 6x a mediana
            P07 +18    5 destinos inéditos em 10 min
                       18 + 18 + 18 = 54, teto do payer_behavior: 45
            P04 +20    5 tentativas em 5 min
            P05 +15    R$ 30.000 em 1 h
                       20 + 15 = 35, teto do payer_velocity: 25
            L02 +10    aparelho sem cadastro, acima de R$ 200
            REL01 +8   destino inédito (eixo do destino)
            pagador 45 + 25 + 10 = 80; destino 8

        Rejeitado: 80 >= 70 com os quatro sinais. Até o 4º a rajada ainda
        não tinha 5 tentativas, e cada um ficou em 2FA.
        """
        payer, usual, new_phone = new_payer(), new_recipient(), new_device()
        seed_pix(payer, usual, days_before(at(T0, 9), 10), amount=100000)
        seed(
            [pix_payload(payer, new_recipient(), moment, amount=600000, device_id=new_phone) for moment in minutes_before(T0, 4)],
            expect="two_factor_required",
        )

        pix = post_pix(pix_payload(payer, new_recipient(), T0, amount=600000, device_id=new_phone))

        assert pix.http_status == 201
        assert pix.status == "rejected"
        assert pix.decision_rule == "D-REJ-ATO"
        assert pix.triggered() == {"P03", "P04", "P05", "P06", "P07", "L02", "REL01"}
        assert pix.scores == {"payer": 80, "recipient": 8}

    def test_ato_sem_destinos_ineditos_fica_em_2fa(self):
        """Sem o P07, a mesma rajada fica em 2FA pelo D-ATO-1: a rejeição pede os quatro sinais.

        O mesmo preparo (os 10 Pix às 9h), com a rajada inteira pro destino
        de sempre: P03, P04, P05 e P06 disparam, o P07 não.

            P03 +18, P06 +18    36 (payer_behavior)
            P04 +20, P05 +15    35 → 25 (payer_velocity)
            L02 +10
            pagador 71
            R02 +16             a mesma rajada, vista do destino: 5 entradas
                                em 10 min; o REL02 some com o 2FA recente
                                no par
            destino 16

        D-ATO-1: 2FA apesar dos 71 — nota alta sem a combinação completa
        não rejeita.
        """
        payer, recipient, new_phone = new_payer(), new_recipient(), new_device()
        seed_pix(payer, recipient, days_before(at(T0, 9), 10), amount=100000)
        seed_pix(payer, recipient, minutes_before(T0, 4), expect="two_factor_required", amount=600000, device_id=new_phone)

        pix = post_pix(pix_payload(payer, recipient, T0, amount=600000, device_id=new_phone))

        assert pix.http_status == 201
        assert pix.status == "two_factor_required"
        assert pix.decision_rule == "D-ATO-1"
        assert pix.triggered() == {"P03", "P04", "P05", "P06", "L02", "R02"}
        assert pix.scores == {"payer": 71, "recipient": 16}

    @pytest.mark.parametrize(
        "occurrences, status, decision_rule, dict_signals, payer_score",
        [
            (EVIDENCE, "rejected", "D-REJ-PAY", set(), 76),
            (CONFIRMED, "rejected", "D-REJ-PAY", set(), 76),
            (REPORT, "two_factor_required", "D-PAY", set(), 76),
            (EXPIRED, "two_factor_required", "D-PAY", set(), 76),
            ([], "two_factor_required", "D-PAY", set(), 76),
        ],
        ids=["evidencia-rejeita", "confirmada-rejeita", "so-denuncia-fica-em-2fa", "vencida-fica-em-2fa", "sem-ocorrencia-fica-em-2fa"],
    )
    def test_rejeicao_do_pagador_pede_evidencia_do_dict_contra_ele(
        self, sources_mock, occurrences, status, decision_rule, dict_signals, payer_score,
    ):
        """D-REJ-PAY — o dreno sem aparelho estranho, com o DICT apontando o próprio pagador (REJ-11, REJ-12).

        O invasor pode estar no celular cadastrado: sem P03 não há
        D-REJ-ATO, e a RFC fecha a conta com a evidência do DICT contra o
        pagador. Preparo: 10 Pix de R$ 1.000 pro destino de sempre, um por
        dia nos 10 dias anteriores, às 9h (mediana R$ 1.000, com o lastro do
        P06). Nos 4 minutos antes de T0, 4 Pix de R$ 50.000 do aparelho
        cadastrado, cada um pra um destino inédito — terminam em 2FA
        (D-SCAM, D-LIMIT ou D-PAY). Pix: o 5º, igual.

            P04 +20    5 tentativas em 5 min
            P05 +15    R$ 250.000 em 1 h
                       20 + 15 = 35, teto do payer_velocity: 25
            P06 +18    R$ 50.000 é 50x a mediana
            P07 +18    5 destinos inéditos em 10 min
            L03 +15    R$ 50.000 é o teto por Pix
            REL01 +8   destino inédito (eixo do destino)
            pagador 25 + 36 + 15 = 76; destino 8

        Com evidência de fraude ou fraude confirmada vigente contra o
        pagador: rejeitado, D-REJ-PAY. Só com denúncia não resolvida, com
        a evidência vencida ou sem ocorrência nenhuma, a porteira não abre
        e os 76 ficam em 2FA, pelo D-PAY. Repare que a nota do pagador é
        a mesma nos cinco casos: a ocorrência do pagador não pontua, só
        corrobora (RFC §7).
        """
        payer, usual = new_payer(), new_recipient()
        seed_pix(payer, usual, days_before(at(T0, 9), 10), amount=100000)
        seed(
            [pix_payload(payer, new_recipient(), moment, amount=5000000) for moment in minutes_before(T0, 4)],
            expect="two_factor_required",
        )
        sources_mock.set_security_statistics(payer.document, 70, occurrences)

        pix = post_pix(pix_payload(payer, new_recipient(), T0, amount=5000000))

        assert pix.http_status == 201
        assert pix.status == status
        assert pix.decision_rule == decision_rule
        assert pix.triggered() == {"P04", "P05", "P06", "P07", "L03", "REL01"} | dict_signals
        assert pix.scores == {"payer": payer_score, "recipient": 8}
        if decision_rule == "D-REJ-PAY":
            assert pix.reasons[0].endswith("com evidência de fraude no DICT contra o pagador")

    @pytest.mark.parametrize(
        "occurrences, dict_signal, recipient_score",
        [(EVIDENCE, "DICT02", 95), (CONFIRMED, "DICT03", 100)],
        ids=["evidencia", "confirmada"],
    )
    def test_rejeicao_do_destino_pede_evidencia_de_fraude_no_dict(self, sources_mock, occurrences, dict_signal, recipient_score):
        """D-REJ-RCP — rajada de muita gente num destino que o DICT aponta (REJ-13).

        Preparo: 4 pagadores novos pagam o titular nos 4 minutos antes de
        T0. O DICT responde estatística 12 e a ocorrência pro documento
        dele. Pix: um 5º pagador, com 1 Pix de ontem pra outro destino.

            R02 +16      5 entradas em 10 min
            R03 +20      5 pagadores em 1 h
                         16 + 20 = 36, teto da rede: 35
            DICT01 +32   estatística 12 (< 30)
            DICT02 +20   evidência de fraude (ou DICT03 +25, confirmada)
            REL01 +8     destino inédito para quem tem histórico
            destino 35 + 32 + 20 + 8 = 95 (evidência) ou 100 (confirmada:
                         a nota para em 100)

        Rejeitado: R02 e R03 juntos, destino >= 55 e a evidência. Sem ela,
        a mesma rede fica em 2FA — os testes seguintes.
        """
        recipient = _crowded_recipient()
        sources_mock.set_security_statistics(recipient.document, 12, occurrences)

        pix = post_pix(pix_payload(_known_payer(), recipient, T0))

        assert pix.http_status == 201
        assert pix.status == "rejected"
        assert pix.decision_rule == "D-REJ-RCP"
        assert pix.triggered() == {"R02", "R03", "DICT01", dict_signal, "REL01"}
        assert pix.scores == {"payer": 0, "recipient": recipient_score}

    @pytest.mark.parametrize(
        "occurrences, dict_down, recipient_score, dict_signals",
        [
            (REPORT, False, 83, {"DICT04"}),
            (EXPIRED, False, 75, set()),
            ([], False, 75, set()),
            ([], True, 43, None),
        ],
        ids=["so-denuncia", "evidencia-vencida", "sem-ocorrencia", "dict-fora-do-ar"],
    )
    def test_rede_do_destino_sem_evidencia_fica_em_2fa(self, sources_mock, occurrences, dict_down, recipient_score, dict_signals):
        """Sem evidência de fraude vigente, a mesma rede pede 2FA pelo D-RCP, e nunca rejeita (REJ-14, REJ-15, REJ-19).

        O mesmo preparo do D-REJ-RCP, com o DICT respondendo estatística 12
        e só uma denúncia não resolvida (DICT04 +8, que pontua e não abre
        porteira), a evidência vencida (nada pontua), nenhuma ocorrência,
        ou fora do ar, com erro 500 (a camada 3 inteira unavailable).

            só denúncia      35 (rede) + 32 (DICT01) + 8 (DICT04) + 8 (REL01) = 83
            vencida / nada   35 + 32 + 8 = 75
            fora do ar       35 + 8 = 43

        D-RCP nos quatro: a rede passa dos 40, mas rejeição de destino só
        com o DICT apontando fraude — denúncia não é evidência, evidência
        vencida não é evidência, e falta de resposta não é nada.
        """
        recipient = _crowded_recipient()
        if dict_down:
            sources_mock.set_status(recipient.document, 500)
        else:
            sources_mock.set_security_statistics(recipient.document, 12, occurrences)

        pix = post_pix(pix_payload(_known_payer(), recipient, T0))

        assert pix.http_status == 201
        assert pix.status == "two_factor_required"
        assert pix.decision_rule == "D-RCP"
        if dict_signals is None:
            assert {pix.signal_status(code) for code in ("DICT01", "DICT02", "DICT03", "DICT04")} == {"unavailable"}
        else:
            assert pix.triggered() == {"R02", "R03", "DICT01", "REL01"} | dict_signals
        assert pix.scores == {"payer": 0, "recipient": recipient_score}

    @pytest.mark.parametrize(
        "occurrences, status, decision_rule, dict_signals, recipient_score",
        [
            (CONFIRMED, "rejected", "D-REJ-CROSS", {"DICT03"}, 65),
            (EVIDENCE, "rejected", "D-REJ-CROSS", {"DICT02"}, 60),
            (REPORT, "two_factor_required", "D-PAY", {"DICT04"}, 48),
            (EXPIRED, "two_factor_required", "D-PAY", set(), 40),
            ([], "two_factor_required", "D-PAY", set(), 40),
        ],
        ids=["confirmada-rejeita", "evidencia-rejeita", "so-denuncia-fica-em-2fa", "vencida-fica-em-2fa", "sem-ocorrencia-fica-em-2fa"],
    )
    def test_rejeicao_cruzada_pede_os_dois_eixos_e_a_evidencia(
        self, sources_mock, occurrences, status, decision_rule, dict_signals, recipient_score,
    ):
        """D-REJ-CROSS — pagador e destino os dois no limiar do 2FA, e o destino apontado no DICT (REJ-04, REJ-05, REJ-06).

        Preparo: o pagador tem 3 Pix nos 3 dias anteriores; nos 4 minutos
        antes de T0, 4 Pix de R$ 2.000, cada um pra um destino inédito
        (aprovados: o P07 sozinho fica em 18). Pix: R$ 2.000 pra mais um
        destino inédito, que o DICT avalia com estatística 12 e a
        ocorrência.

            P04 +20      5 tentativas em 5 min
            P05 +15      R$ 10.000 em 1 h
                         20 + 15 = 35, teto do payer_velocity: 25
            P07 +18      5 destinos inéditos em 10 min
            pagador 25 + 18 = 43
            DICT01 +32   estatística 12 (< 30)
            DICT03 +25   fraude confirmada (ou DICT02 +20, evidência; ou
                         DICT04 +8, denúncia, que não abre porteira)
            REL01 +8     destino inédito
            destino 32 + 25 + 8 = 65 (ou 60; só denúncia, 48)

        Rejeitado: 43 e o destino passam dos 40, com a evidência. Só com
        a denúncia, com a evidência vencida ou sem ocorrência, o mesmo
        Pix sai em 2FA pelo D-PAY — a linha do pagador vem antes da do
        destino, e a vencida nem pontua (REJ-06).
        """
        payer = new_payer()
        seed_pix(payer, new_recipient(), days_before(T0, 3))
        seed([pix_payload(payer, new_recipient(), moment, amount=200000) for moment in minutes_before(T0, 4)])
        recipient = new_recipient()
        sources_mock.set_security_statistics(recipient.document, 12, occurrences)

        pix = post_pix(pix_payload(payer, recipient, T0, amount=200000))

        assert pix.http_status == 201
        assert pix.status == status
        assert pix.decision_rule == decision_rule
        assert pix.triggered() == {"P04", "P05", "P07", "DICT01", "REL01"} | dict_signals
        assert pix.scores == {"payer": 43, "recipient": recipient_score}
