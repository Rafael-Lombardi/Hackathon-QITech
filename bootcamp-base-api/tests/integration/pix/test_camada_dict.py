"""Camada 3: a estatística de segurança e as ocorrências do DICT, pelo mock dele.

A API consulta GET /security-statistics/{documento} pelo recebedor e pelo
pagador antes de decidir, e a resposta traz as duas coisas. Do recebedor:
estatística abaixo de 30 dispara o DICT01 (+32, eixo do destino, com teto
próprio); uma ocorrência vigente dispara o sinal do tipo dela — evidência
de fraude, DICT02 (+20); fraude confirmada na fixture, DICT03 (+25);
denúncia não resolvida, DICT04 (+8) —, os três no grupo dict_security
(teto 25). Do pagador, ocorrência nenhuma pontua: é só a porteira do
D-REJ-PAY (test_rejeicoes.py). Mock lento demais, com erro ou respondendo
torto vira a camada 3 inteira unavailable — e o Pix sai decidido pelas
outras duas camadas, sem ponto e sem desconto. Uma ocorrência torta, ao
contrário, é só ignorada: as outras valem.

Cada teste escreve o roteiro do recebedor dele no mock pela fixture
sources_mock (tests/conftest.py), que apaga o roteiro quando o teste acaba,
e, quando o assunto é quem foi consultado, pergunta ao mock depois. Os
outros arquivos quase não mexem no DICT do mock: lá vale a expectativa
padrão, confiança 70, e o DICT01 fica calado. As fronteiras 39/40 do D-RCP, que
usam o DICT01, estão em test_fronteiras.py.
"""

import math
import time
from datetime import timedelta

import pytest

from tests.utils.clock import T0, at, days_ago
from tests.utils.identities import format_document, new_payer, new_recipient
from tests.utils.pix import pix_payload, post_pix, seed_pix
from tests.utils.sources_mock import occurrence, sources_timeout

LOW_SECURITY_STATISTICS = 12

# O que fica calado no primeiro Pix de um pagador coberto pelo cadastro,
# com o DICT respondendo sem nada contra o destino.
QUIET_ON_FIRST_PIX = {"P01", "L01", "R01", "REL03", "L02", "L03", "P03", "DICT01", "DICT02", "DICT03", "DICT04"}


class TestCamadaDict:
    @pytest.mark.parametrize(
        "statistics, expected, points",
        [(29, "triggered", 32), (30, "not_triggered", 0)],
        ids=["confianca-29-dispara", "confianca-30-nao-dispara"],
    )
    def test_fronteira_dict01_estatistica_abaixo_de_30(self, sources_mock, statistics, expected, points):
        """DICT01: confiança do destino abaixo de 30. Com 29, +32; com 30, calado.

        Pagador novo na base, e por isso o REL01 fica unavailable: o DICT01
        está sozinho no eixo do destino. 32 não chega aos 40 do D-RCP, de
        propósito — o DICT real atualiza com até 12 h de atraso, e uma nota
        de fora, sozinha, não pede 2FA. D-OK nos dois lados.
        """
        payer, recipient = new_payer(), new_recipient()
        sources_mock.set_security_statistics(recipient.document, statistics)

        pix = post_pix(pix_payload(payer, recipient, T0))

        assert pix.http_status == 201
        assert pix.signal_status("DICT01") == expected
        assert pix.signal("DICT01")["points"] == points
        assert pix.scores["recipient"] == points
        assert pix.status == "approved"
        if expected == "triggered":
            assert pix.signal("DICT01")["evidence"] == {"security_statistics": 29, "threshold": 30}

    def test_primeiro_pix_entre_contas_novas_com_dict_baixo_pede_2fa(self, sources_mock):
        """Conta nova pagando conta nova que o DICT avalia mal: 2FA já no primeiro Pix.

        Um cliente novo, de conta aberta ontem, manda R$ 1 milhão às 3h
        pra uma conta aberta ontem, com estatística 12 no DICT. Sem
        histórico, só a camada 1, o teto por Pix e o DICT têm o que dizer:

            P01 +10      conta pagadora com 1 dia
            L01 +8       3h, acima de R$ 1.000
            L03 +15      R$ 1 milhão de quem nunca teve Pix aprovado (teto R$ 5.000)
            R01 +12      conta de destino com 1 dia
            DICT01 +32   estatística 12 (< 30), com teto próprio
            pagador 33, destino 12 + 32 = 44

        2FA pelo D-LIMIT, a linha do teto, que vem antes da do destino. O
        destino sozinho também pediria (44 >= 40, D-RCP), e é esse o
        ponto do teto próprio do DICT01: a nota do DICT é o que o sistema
        Pix inteiro viu do destino, e não divide teto com a rede que só
        nós vemos.
        """
        payer = new_payer(opening_date=T0 - timedelta(days=1))
        recipient = new_recipient(opening_date=T0 - timedelta(days=1))
        sources_mock.set_security_statistics(recipient.document, LOW_SECURITY_STATISTICS)

        pix = post_pix(pix_payload(payer, recipient, at(T0, 3), amount=100_000_000))

        assert pix.http_status == 201
        assert pix.triggered() == {"P01", "L01", "L03", "R01", "DICT01"}
        assert pix.scores == {"payer": 33, "recipient": 44}
        assert pix.status == "two_factor_required"
        assert pix.decision_rule == "D-LIMIT"

    def test_relacao_recorrente_segura_o_2fa_da_conta_nova_com_dict_baixo(self, sources_mock):
        """Quem já paga este titular há 10 dias não leva 2FA quando ele abre conta nova mal avaliada.

        Preparo: 3 Pix aprovados pro mesmo titular, há 10, 5 e 1 dia, na
        conta antiga dele. Pix: pra conta nova do titular, aberta há 2
        dias, com confiança 12 no DICT.

            R01 +12      conta de destino com 2 dias
            DICT01 +32   confiança 12 (< 30)
            REL02 -8     relação recorrente com o titular
            destino 12 + 32 - 8 = 36

        D-OK. Sem a relação, os mesmos R01 e DICT01 dão 44 e pedem 2FA de
        qualquer pagador — o teste anterior.
        """
        payer, holder = new_payer(), new_recipient()
        seed_pix(payer, holder, days_ago(T0, 10, 5, 1))
        new_account = holder.with_new_account(opening_date=T0 - timedelta(days=2))
        sources_mock.set_security_statistics(holder.document, LOW_SECURITY_STATISTICS)

        pix = post_pix(pix_payload(payer, new_account, T0))

        assert pix.http_status == 201
        assert pix.triggered() == {"R01", "DICT01", "REL02"}
        assert pix.scores["recipient"] == 36
        assert pix.status == "approved"
        assert pix.decision_rule == "D-OK"

    @pytest.mark.parametrize(
        "kind, triggered, points",
        [("fraud_related_evidence", "DICT02", 20), ("confirmed_in_mock", "DICT03", 25), ("unresolved_report", "DICT04", 8)],
        ids=["evidencia-dispara-dict02", "confirmada-dispara-dict03", "denuncia-dispara-dict04"],
    )
    def test_ocorrencia_vigente_vira_dict02_dict03_ou_dict04(self, sources_mock, kind, triggered, points):
        """Uma ocorrência vigente contra o destino vira o sinal do tipo dela: DICT02 (+20), DICT03 (+25) ou DICT04 (+8).

        A estatística fica no padrão (70) e o pagador é novo na base, do
        aparelho cadastrado: no eixo do destino só a ocorrência pesa, e os
        outros dois sinais do grupo ficam calados — o DICT respondeu, e a
        ocorrência é de um tipo só. A evidência traz o id dela. Sozinha,
        nenhuma chega aos 40 do D-RCP: D-OK. O que a evidência e a
        confirmação abrem é a porteira das rejeições do destino, em
        test_rejeicoes.py; a denúncia não resolvida não abre nada.
        """
        payer, recipient = new_payer(), new_recipient()
        found = occurrence(kind)
        sources_mock.set_security_statistics(recipient.document, 70, [found])

        pix = post_pix(pix_payload(payer, recipient, T0))

        assert pix.http_status == 201
        assert pix.triggered() == {triggered}
        assert pix.signal(triggered)["evidence"] == {"occurrences": [found["occurrence_id"]]}
        assert pix.signals_with("not_triggered") == QUIET_ON_FIRST_PIX - {triggered}
        assert pix.scores == {"payer": 0, "recipient": points}
        assert pix.status == "approved"

    def test_tres_ocorrencias_pesam_como_a_mais_grave(self, sources_mock):
        """Evidência, confirmação e denúncia juntas: os três sinais disparam, e o grupo para no teto de 25.

        20 + 25 + 8 = 53 brutos, 25 aplicados (family_contributions mostra
        os dois): um documento com tudo contra ele pesa como a fraude
        confirmada, e não três vezes (RFC §6). Com a estatística 12, o
        destino chega a 32 + 25 = 57: D-RCP, 2FA — e não rejeição, que
        pede também a rede do destino ou o pagador no limiar.
        """
        payer, recipient = new_payer(), new_recipient()
        sources_mock.set_security_statistics(recipient.document, 12, [
            occurrence("fraud_related_evidence"), occurrence("confirmed_in_mock"), occurrence("unresolved_report"),
        ])

        pix = post_pix(pix_payload(payer, recipient, T0))

        assert pix.http_status == 201
        assert pix.triggered() == {"DICT01", "DICT02", "DICT03", "DICT04"}
        assert pix.metadata["family_contributions"]["dict_security"] == {"raw": 53, "applied": 25, "cap": 25}
        assert pix.scores == {"payer": 0, "recipient": 57}
        assert pix.status == "two_factor_required"
        assert pix.decision_rule == "D-RCP"

    @pytest.mark.parametrize(
        "reported_at, expires_at, expected",
        [
            (T0 - timedelta(days=30), T0 - timedelta(hours=1), "not_triggered"),
            (T0 - timedelta(days=30), T0, "not_triggered"),
            (T0 + timedelta(hours=1), T0 + timedelta(days=30), "not_triggered"),
            (T0, T0 + timedelta(days=30), "triggered"),
        ],
        ids=["vencida-ha-1h", "vence-neste-instante", "registrada-daqui-a-1h", "registrada-neste-instante"],
    )
    def test_ocorrencia_so_vale_dentro_da_vigencia(self, sources_mock, reported_at, expires_at, expected):
        """E1 — uma ocorrência vale em t sse reported_at <= t < expires_at (RFC §6).

        A fraude confirmada contra o destino, com estas datas; t é o
        instante do Pix (T0). Vencida, vencendo neste exato instante ou
        ainda não registrada, o DICT03 fica calado e nenhum ponto entra —
        a ocorrência expirada não é evidência. Registrada neste exato
        instante, já vale.
        """
        payer, recipient = new_payer(), new_recipient()
        sources_mock.set_security_statistics(
            recipient.document, 70, [occurrence("confirmed_in_mock", reported_at=reported_at, expires_at=expires_at)],
        )

        pix = post_pix(pix_payload(payer, recipient, T0))

        assert pix.http_status == 201
        assert pix.signal_status("DICT03") == expected
        assert pix.status == "approved"

    def test_ocorrencia_duplicada_conta_uma_vez(self, sources_mock):
        """E1 — a mesma ocorrência (mesma fonte, mesmo id) duas vezes na resposta é uma só.

        A evidência do DICT03 traz o id uma vez. Com o teto, a duplicata
        nem mudaria a nota; o que ela mudaria é a história que a resposta
        conta, e a RFC pede deduplicação por fonte e id (§6).
        """
        payer, recipient = new_payer(), new_recipient()
        found = occurrence("confirmed_in_mock", occurrence_id="occ-repetida")
        sources_mock.set_security_statistics(recipient.document, 70, [found, dict(found)])

        pix = post_pix(pix_payload(payer, recipient, T0))

        assert pix.http_status == 201
        assert pix.signal("DICT03")["evidence"] == {"occurrences": ["occ-repetida"]}
        assert pix.scores["recipient"] == 25

    @pytest.mark.parametrize(
        "broken",
        [
            occurrence("suspicious"),
            occurrence("confirmed_in_mock", expires_at=None),
            occurrence("confirmed_in_mock", expires_at="amanhã"),
            occurrence("confirmed_in_mock", reported_at="2026-01-01T00:00:00"),
            {"kind": "confirmed_in_mock", "reported_at": "2026-01-01T00:00:00-03:00", "expires_at": "2030-01-01T00:00:00-03:00"},
            "confirmed_in_mock",
        ],
        ids=["tipo-desconhecido", "sem-prazo", "prazo-que-nao-e-data", "registro-sem-fuso", "sem-id-nem-fonte", "texto-solto"],
    )
    def test_ocorrencia_torta_e_ignorada_e_o_resto_da_resposta_vale(self, sources_mock, broken):
        """Uma ocorrência fora do combinado é ignorada, e a resposta continua valendo: estatística e as outras ocorrências.

        Tipo que não conhecemos, sem prazo, prazo que não é data, registro
        sem fuso, sem id ou fonte, ou nem um objeto: fica no log, e fora
        do Context. A estatística 12 dispara o DICT01 e a confirmação boa,
        no mesmo corpo, dispara o DICT03 — a RFC manda ignorar a
        ocorrência sem prazo, não a resposta inteira (§6).
        """
        payer, recipient = new_payer(), new_recipient()
        good = occurrence("confirmed_in_mock")
        sources_mock.set_body(recipient.document, {"security_statistics": 12, "occurrences": [broken, good]})

        pix = post_pix(pix_payload(payer, recipient, T0))

        assert pix.http_status == 201
        assert pix.signal_status("DICT01") == "triggered"
        assert pix.signal("DICT03")["evidence"] == {"occurrences": [good["occurrence_id"]]}
        assert {pix.signal_status(code) for code in ("DICT02", "DICT04")} == {"not_triggered"}

    def test_occurrences_que_nao_e_lista_vira_dict_indisponivel(self, sources_mock):
        """occurrences que não é lista é resposta fora do combinado: a camada 3 inteira sai unavailable.

        Vale também pra estatística que veio junto — a API não aproveita
        metade de uma resposta que não entendeu. A estatística 12 no mesmo
        corpo dispararia o DICT01 se fosse lida. D-OK.
        """
        payer, recipient = new_payer(), new_recipient()
        sources_mock.set_body(recipient.document, {"security_statistics": 12, "occurrences": "nenhuma"})

        pix = post_pix(pix_payload(payer, recipient, T0))

        assert pix.http_status == 201
        assert {pix.signal_status(code) for code in ("DICT01", "DICT02", "DICT03", "DICT04")} == {"unavailable"}
        assert pix.status == "approved"

    def test_ocorrencia_contra_o_pagador_nao_pontua(self, sources_mock):
        """A evidência do DICT contra o PAGADOR não vira ponto em eixo nenhum: é só a porteira do D-REJ-PAY.

        O pagador tem fraude confirmada no DICT; o destino está limpo. Os
        quatro sinais do DICT ficam calados (são do eixo do destino),
        pagador 0, destino 0, D-OK — a RFC é explícita: a evidência do
        pagador corrobora a rejeição, e não adiciona pontos (§7). O
        D-REJ-PAY, com a rajada que a porteira exige, está em
        test_rejeicoes.py.
        """
        payer, recipient = new_payer(), new_recipient()
        sources_mock.set_security_statistics(payer.document, 70, [occurrence("confirmed_in_mock")])

        pix = post_pix(pix_payload(payer, recipient, T0))

        assert pix.http_status == 201
        assert pix.triggered() == set()
        assert pix.scores == {"payer": 0, "recipient": 0}
        assert pix.status == "approved"
        sources_mock.verify_called(payer.document, times=1)

    def test_mock_lento_vira_unavailable_e_a_decisao_sai(self, sources_mock):
        """O DICT que demora mais que o prazo não segura o Pix.

        O mock responde só depois do prazo da API (SOURCES_TIMEOUT, 2 s)
        mais 2 s, com a confiança 0 do set_delay. A API desiste no prazo:
        DICT01 unavailable, o Pix decidido pelas outras camadas (D-OK), e
        a resposta chega antes da do mock. É o único teste da suíte que
        espera relógio: os 2 s do connector.
        """
        payer, recipient = new_payer(), new_recipient()
        timeout = sources_timeout()
        sources_mock.set_delay(recipient.document, math.ceil(timeout) + 2, statistics=0)

        started = time.monotonic()
        pix = post_pix(pix_payload(payer, recipient, T0))
        elapsed = time.monotonic() - started

        assert pix.http_status == 201
        assert pix.signal_status("DICT01") == "unavailable"
        assert pix.status == "approved"
        assert pix.decision_rule == "D-OK"
        assert elapsed < timeout + 1.5, f"a API esperou {elapsed:.1f} s pelo DICT"

    @pytest.mark.parametrize("status_code", [404, 500, 503], ids=["404", "500", "503"])
    def test_mock_com_erro_vira_unavailable(self, sources_mock, status_code):
        """DICT respondendo erro é DICT indisponível: DICT01 unavailable, e o Pix sai.

        O corpo do erro traz confiança 0 (o set_status explica por quê).
        """
        payer, recipient = new_payer(), new_recipient()
        sources_mock.set_status(recipient.document, status_code)

        pix = post_pix(pix_payload(payer, recipient, T0))

        assert pix.http_status == 201
        assert pix.signal_status("DICT01") == "unavailable"
        assert pix.status == "approved"

    @pytest.mark.parametrize(
        "body",
        [
            {"security_statistics": 150},
            {"security_statistics": -1},
            {"security_statistics": "12"},
            {"security_statistics": 12.5},
            {},
            "confianca alta",
        ],
        ids=["acima-de-100", "negativo", "em-texto", "fracionario", "sem-security-statistics", "corpo-que-nao-e-json"],
    )
    def test_resposta_torta_do_mock_vira_unavailable(self, sources_mock, body):
        """Só um inteiro de 0 a 100 é confiança. O resto é DICT indisponível.

        Confiança fora da faixa, em texto, fracionária, ausente ou um corpo
        que nem é JSON: nenhum vira número por conversão, e nenhum derruba
        o Pix. DICT01 unavailable, D-OK.
        """
        payer, recipient = new_payer(), new_recipient()
        sources_mock.set_body(recipient.document, body)

        pix = post_pix(pix_payload(payer, recipient, T0))

        assert pix.http_status == 201
        assert pix.signal_status("DICT01") == "unavailable"
        assert pix.status == "approved"

    def test_pagador_e_recebedor_sao_consultados_uma_vez_cada(self, sources_mock):
        """A camada 3 pergunta pelo destino e pelo pagador: duas consultas por Pix, uma de cada.

        Do destino saem os pontos (DICT01 a DICT04) e a porteira das
        rejeições do destino; do pagador, só a porteira do D-REJ-PAY. Pra
        si mesmo (REL03) é um documento só, e uma consulta.
        """
        payer, recipient = new_payer(), new_recipient()

        pix = post_pix(pix_payload(payer, recipient, T0))

        assert pix.http_status == 201
        sources_mock.verify_called(recipient.document, times=1)
        sources_mock.verify_called(payer.document, times=1)

    def test_dict_recebe_o_documento_so_com_digitos(self, sources_mock):
        """O documento chega pontuado no payload e vai ao DICT só com dígitos.

        A confiança 12 está no roteiro do documento sem pontuação: se a API
        consultasse com ela, cairia na expectativa padrão (70) e o DICT01
        ficaria calado.
        """
        payer, recipient = new_payer(), new_recipient()
        sources_mock.set_security_statistics(recipient.document, LOW_SECURITY_STATISTICS)

        pix = post_pix(pix_payload(payer, recipient, T0, recipient_document=format_document(recipient.document)))

        assert pix.http_status == 201
        assert pix.signal_status("DICT01") == "triggered"
        sources_mock.verify_called(recipient.document, times=1)

    def test_reenvio_nao_consulta_o_dict_de_novo(self, sources_mock):
        """O reenvio idêntico devolve a análise gravada sem ir ao DICT.

        A idempotência vem antes de qualquer I/O (o passo 2 do
        PixTransactionController.create): duas chegadas do mesmo Pix, uma
        consulta só.
        """
        payer, recipient = new_payer(), new_recipient()
        payload = pix_payload(payer, recipient, T0)

        first = post_pix(payload)
        again = post_pix(payload)

        assert first.http_status == 201
        assert again.http_status == 200
        sources_mock.verify_called(recipient.document, times=1)
