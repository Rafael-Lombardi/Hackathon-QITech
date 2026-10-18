"""Camada 3: a confiança do destino, pelo mock do DICT.

A API consulta GET /trust-factor/{documento do recebedor} antes de decidir.
Confiança abaixo de 30 dispara o DICT01 (+32, eixo do destino, no grupo
recipient_trust, com teto próprio). Mock lento demais, com erro ou
respondendo torto vira DICT01 unavailable — e o Pix sai decidido pelas
outras duas camadas, sem ponto e sem desconto.

Cada teste escreve o roteiro do recebedor dele no mock pela fixture
dict_mock (tests/conftest.py), que apaga o roteiro quando o teste acaba,
e, quando o assunto é quem foi consultado, pergunta ao mock depois. Os
outros arquivos quase não mexem no mock: lá vale a expectativa padrão,
confiança 70, e o DICT01 fica calado. As fronteiras 39/40 do D-RCP, que
usam o DICT01, estão em test_fronteiras.py.
"""

import math
import time
from datetime import timedelta

import pytest

from tests.utils.clock import T0, at, days_ago
from tests.utils.dict_mock import dict_timeout
from tests.utils.identities import format_document, new_payer, new_recipient
from tests.utils.pix import pix_payload, post_pix, seed_pix

LOW_TRUST = 12


class TestCamadaDict:
    @pytest.mark.parametrize(
        "trust, expected, points",
        [(29, "triggered", 32), (30, "not_triggered", 0)],
        ids=["confianca-29-dispara", "confianca-30-nao-dispara"],
    )
    def test_fronteira_dict01_confianca_abaixo_de_30(self, dict_mock, trust, expected, points):
        """DICT01: confiança do destino abaixo de 30. Com 29, +32; com 30, calado.

        Pagador novo na base, e por isso o REL01 fica unavailable: o DICT01
        está sozinho no eixo do destino. 32 não chega aos 40 do D-RCP, de
        propósito — o DICT real atualiza com até 12 h de atraso, e uma nota
        de fora, sozinha, não pede 2FA. D-OK nos dois lados.
        """
        payer, recipient = new_payer(), new_recipient()
        dict_mock.set_trust(recipient.document, trust)

        pix = post_pix(pix_payload(payer, recipient, T0))

        assert pix.http_status == 201
        assert pix.signal_status("DICT01") == expected
        assert pix.signal("DICT01")["points"] == points
        assert pix.scores["recipient"] == points
        assert pix.status == "approved"
        if expected == "triggered":
            assert pix.signal("DICT01")["evidence"] == {"trust_factor": 29, "threshold": 30}

    def test_primeiro_pix_entre_contas_novas_com_dict_baixo_pede_2fa(self, dict_mock):
        """Conta nova pagando conta nova que o DICT avalia mal: 2FA já no primeiro Pix.

        Um cliente novo, de conta aberta ontem, manda R$ 1 milhão às 3h
        pra uma conta aberta ontem, com confiança 12 no DICT. Sem histórico,
        só a camada 1 e o DICT têm o que dizer:

            P01 +10      conta pagadora com 1 dia
            L01 +8       3h, acima de R$ 1.000
            R01 +12      conta de destino com 1 dia
            DICT01 +32   confiança 12 (< 30), no recipient_trust
            pagador 18, destino 12 + 32 = 44

        D-RCP. O DICT01 tem teto próprio na política v0.5:
        dividindo o teto de 35 com o R01, este Pix parava em 35 e saía
        aprovado.
        """
        payer = new_payer(opening_date=T0 - timedelta(days=1))
        recipient = new_recipient(opening_date=T0 - timedelta(days=1))
        dict_mock.set_trust(recipient.document, LOW_TRUST)

        pix = post_pix(pix_payload(payer, recipient, at(T0, 3), amount=100_000_000))

        assert pix.http_status == 201
        assert pix.triggered() == {"P01", "L01", "R01", "DICT01"}
        assert pix.scores == {"payer": 18, "recipient": 44}
        assert pix.status == "two_factor_required"
        assert pix.decision_rule == "D-RCP"

    def test_relacao_recorrente_segura_o_2fa_da_conta_nova_com_dict_baixo(self, dict_mock):
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
        dict_mock.set_trust(holder.document, LOW_TRUST)

        pix = post_pix(pix_payload(payer, new_account, T0))

        assert pix.http_status == 201
        assert pix.triggered() == {"R01", "DICT01", "REL02"}
        assert pix.scores["recipient"] == 36
        assert pix.status == "approved"
        assert pix.decision_rule == "D-OK"

    def test_mock_lento_vira_unavailable_e_a_decisao_sai(self, dict_mock):
        """O DICT que demora mais que o prazo não segura o Pix.

        O mock responde só depois do prazo da API (DICT_API_TIMEOUT, 2 s)
        mais 2 s, com a confiança 0 do set_delay. A API desiste no prazo:
        DICT01 unavailable, o Pix decidido pelas outras camadas (D-OK), e
        a resposta chega antes da do mock. É o único teste da suíte que
        espera relógio: os 2 s do connector.
        """
        payer, recipient = new_payer(), new_recipient()
        timeout = dict_timeout()
        dict_mock.set_delay(recipient.document, math.ceil(timeout) + 2, trust=0)

        started = time.monotonic()
        pix = post_pix(pix_payload(payer, recipient, T0))
        elapsed = time.monotonic() - started

        assert pix.http_status == 201
        assert pix.signal_status("DICT01") == "unavailable"
        assert pix.status == "approved"
        assert pix.decision_rule == "D-OK"
        assert elapsed < timeout + 1.5, f"a API esperou {elapsed:.1f} s pelo DICT"

    @pytest.mark.parametrize("status_code", [404, 500, 503], ids=["404", "500", "503"])
    def test_mock_com_erro_vira_unavailable(self, dict_mock, status_code):
        """DICT respondendo erro é DICT indisponível: DICT01 unavailable, e o Pix sai.

        O corpo do erro traz confiança 0 (o set_status explica por quê).
        """
        payer, recipient = new_payer(), new_recipient()
        dict_mock.set_status(recipient.document, status_code)

        pix = post_pix(pix_payload(payer, recipient, T0))

        assert pix.http_status == 201
        assert pix.signal_status("DICT01") == "unavailable"
        assert pix.status == "approved"

    @pytest.mark.parametrize(
        "body",
        [
            {"trust_factor": 150},
            {"trust_factor": -1},
            {"trust_factor": "12"},
            {"trust_factor": 12.5},
            {},
            "confianca alta",
        ],
        ids=["acima-de-100", "negativo", "em-texto", "fracionario", "sem-trust-factor", "corpo-que-nao-e-json"],
    )
    def test_resposta_torta_do_mock_vira_unavailable(self, dict_mock, body):
        """Só um inteiro de 0 a 100 é confiança. O resto é DICT indisponível.

        Confiança fora da faixa, em texto, fracionária, ausente ou um corpo
        que nem é JSON: nenhum vira número por conversão, e nenhum derruba
        o Pix. DICT01 unavailable, D-OK.
        """
        payer, recipient = new_payer(), new_recipient()
        dict_mock.set_body(recipient.document, body)

        pix = post_pix(pix_payload(payer, recipient, T0))

        assert pix.http_status == 201
        assert pix.signal_status("DICT01") == "unavailable"
        assert pix.status == "approved"

    def test_so_o_recebedor_e_consultado(self, dict_mock):
        """A camada 3 pergunta pelo destino, e só por ele: uma consulta por Pix.

        "Quem recebe é confiável?" é a pergunta da família Contraparte. O
        pagador é cliente nosso, e o que sabemos dele vem do histórico.
        """
        payer, recipient = new_payer(), new_recipient()

        pix = post_pix(pix_payload(payer, recipient, T0))

        assert pix.http_status == 201
        dict_mock.verify_called(recipient.document, times=1)
        dict_mock.verify_not_called(payer.document)

    def test_dict_recebe_o_documento_so_com_digitos(self, dict_mock):
        """O documento chega pontuado no payload e vai ao DICT só com dígitos.

        A confiança 12 está no roteiro do documento sem pontuação: se a API
        consultasse com ela, cairia na expectativa padrão (70) e o DICT01
        ficaria calado.
        """
        payer, recipient = new_payer(), new_recipient()
        dict_mock.set_trust(recipient.document, LOW_TRUST)

        pix = post_pix(pix_payload(payer, recipient, T0, recipient_document=format_document(recipient.document)))

        assert pix.http_status == 201
        assert pix.signal_status("DICT01") == "triggered"
        dict_mock.verify_called(recipient.document, times=1)

    def test_reenvio_nao_consulta_o_dict_de_novo(self, dict_mock):
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
        dict_mock.verify_called(recipient.document, times=1)
