"""O cadastro de aparelhos do banco: a fonte do P03, e o que o motor faz quando ela não responde.

O P03 não é histórico: pergunta ao cadastro do banco se o par
pagador–aparelho está cadastrado e vigente neste instante (RFC §3). A
fixture do cadastro entra pelo mock — new_payer cobre o pagador e
registra o aparelho de sempre; sources_mock registra, revoga e derruba
—, nunca por POST: tentativa não cadastra aparelho (test_historico.py).
Quatro respostas, três efeitos: registered e unregistered decidem o P03;
uncovered (pagador fora da fixture) e unavailable (fonte fora do ar)
deixam P03, L02 e P10 unavailable, e só o unavailable troca o D-OK pelo
D-DATA-UNAVAILABLE — ausência em fixture parcial não é não-cadastro, e
fonte fora do ar não é indício contra ninguém.
"""

from datetime import timedelta

import pytest

from tests.utils.clock import T0, days_before
from tests.utils.identities import DEVICE_REGISTERED_AT, new_device, new_payer, new_recipient
from tests.utils.pix import get_pix, pix_payload, post_pix, seed_pix

REGISTRY_SIGNALS = ("P03", "L02", "P10")


class TestCadastroAparelhos:
    def test_pagador_fora_da_fixture_deixa_p03_unavailable_e_aprova(self):
        """Pagador que a fixture do cadastro não cobre: P03, L02 e P10 unavailable, e o Pix aprova.

        Preparo: um pagador fora da fixture (o cadastro responde 404 pra
        qualquer aparelho dele) manda 3 Pix nos 3 dias anteriores. Pix: o
        4º, do mesmo aparelho e IP.

            P03, L02, P10   unavailable: não se sabe se o aparelho é dele
            P09 calado      o IP tem 3 Pix aprovados
            pagador 0

        D-OK, e não D-DATA-UNAVAILABLE: a fonte respondeu ("não cubro
        esse pagador"), e dado parcial não é fonte fora do ar. A resposta
        diz uncovered, sem proveniência, e que o par já tinha aparecido.
        """
        payer, recipient = new_payer(covered=False), new_recipient()
        seed_pix(payer, recipient, days_before(T0, 3))

        pix = post_pix(pix_payload(payer, recipient, T0))

        assert pix.http_status == 201
        assert {pix.signal_status(code) for code in REGISTRY_SIGNALS} == {"unavailable"}
        assert pix.signal_status("P09") == "not_triggered"
        assert pix.scores == {"payer": 0, "recipient": 0}
        assert pix.status == "approved"
        assert pix.decision_rule == "D-OK"
        assert pix.metadata["device"] == {
            "registration_status": "uncovered", "registration_source": None, "previously_observed": True,
        }

    def test_cadastro_fora_do_ar_troca_o_d_ok_pelo_d_data_unavailable(self, sources_mock):
        """Cadastro fora do ar num Pix que aprovaria: 2FA pelo D-DATA-UNAVAILABLE, sem ponto inventado.

        Preparo: 3 Pix nos 3 dias anteriores, do aparelho cadastrado;
        depois o cadastro passa a responder 503 pra este pagador. Pix: o
        de sempre, R$ 50 pro destino de sempre.

            P03, L02, P10   unavailable: a fonte não respondeu
            pagador 0, destino 0

        Nada casa até o fim da tabela, e aprovar seria aprovar sem saber
        de que aparelho o Pix saiu: D-DATA-UNAVAILABLE, 2FA, com a nota em
        0 — fonte fora do ar não vira ponto.
        """
        payer, recipient = new_payer(), new_recipient()
        seed_pix(payer, recipient, days_before(T0, 3))
        sources_mock.registry_down(payer.document)

        pix = post_pix(pix_payload(payer, recipient, T0))

        assert pix.http_status == 201
        assert {pix.signal_status(code) for code in REGISTRY_SIGNALS} == {"unavailable"}
        assert pix.scores == {"payer": 0, "recipient": 0}
        assert pix.status == "two_factor_required"
        assert pix.decision_rule == "D-DATA-UNAVAILABLE"
        assert pix.reasons == ["Cadastro de aparelhos indisponível: confirmação em vez de aprovar no escuro"]
        assert pix.metadata["device"] == {
            "registration_status": "unavailable", "registration_source": None, "previously_observed": True,
        }

    def test_cadastro_fora_do_ar_nao_se_sobrepoe_a_uma_linha_de_cima(self, sources_mock):
        """O fallback só entra no lugar do D-OK: um Pix que já pede 2FA pelo destino fica no D-RCP.

        Preparo: o cadastro responde 503 pra este pagador; o destino é
        uma conta aberta há 2 dias que o DICT avalia com confiança 12. Pix:
        o primeiro do pagador.

            P03, L02, P10   unavailable
            R01 +12         conta de destino com 2 dias
            DICT01 +32      confiança 12 (< 30)
            destino 44

        D-RCP: a linha do destino casa antes, e o fallback nem é olhado.
        """
        payer = new_payer()
        fresh = new_recipient(opening_date=T0 - timedelta(days=2))
        sources_mock.registry_down(payer.document)
        sources_mock.set_security_statistics(fresh.document, 12)

        pix = post_pix(pix_payload(payer, fresh, T0))

        assert pix.http_status == 201
        assert {pix.signal_status(code) for code in REGISTRY_SIGNALS} == {"unavailable"}
        assert pix.triggered() == {"R01", "DICT01"}
        assert pix.scores == {"payer": 0, "recipient": 44}
        assert pix.status == "two_factor_required"
        assert pix.decision_rule == "D-RCP"

    @pytest.mark.parametrize(
        "registered_at, revoked_at, expected",
        [
            (DEVICE_REGISTERED_AT, T0 - timedelta(hours=1), "triggered"),
            (DEVICE_REGISTERED_AT, T0, "triggered"),
            (T0 + timedelta(hours=1), None, "triggered"),
            (T0, None, "not_triggered"),
        ],
        ids=[
            "revogado-ha-1h-dispara", "revogado-neste-instante-dispara",
            "cadastro-so-daqui-a-1h-dispara", "cadastrado-neste-instante-nao-dispara",
        ],
    )
    def test_cadastro_tem_vigencia(self, sources_mock, registered_at, revoked_at, expected):
        """Cadastrado é cadastrado AGORA: registered_at <= t e (sem revoked_at ou revoked_at > t).

        O mesmo Pix de R$ 50, de um aparelho que o banco cadastrou com
        estas datas. Revogado há 1 h, ou cadastrado só daqui a 1 h, é
        aparelho sem cadastro neste instante (P03 +18); revogado neste
        exato instante já não vale; cadastrado neste exato instante já
        vale. Nos quatro casos o pagador não tem histórico (P09
        unavailable), então nenhum D-ATO casa: D-OK.
        """
        payer, phone = new_payer(), new_device()
        sources_mock.register_device(payer.document, phone, registered_at=registered_at, revoked_at=revoked_at)

        pix = post_pix(pix_payload(payer, new_recipient(), T0, device_id=phone))

        assert pix.http_status == 201
        assert pix.signal_status("P03") == expected
        assert pix.status == "approved"
        assert pix.decision_rule == "D-OK"

    def test_metadata_device_separa_cadastro_de_observacao(self):
        """metadata.device diz o que o cadastro respondeu e, à parte, se o par já apareceu; o GET repete.

        Três Pix do mesmo pagador: do aparelho cadastrado (registered,
        nunca observado), de um aparelho sem cadastro (unregistered, nunca
        observado) e de novo dele (unregistered, já observado). Observado
        não vira cadastrado — a regressão B da RFC: registrado mas nunca
        observado, observado sem registro —, e o GET devolve o que foi
        gravado na hora.
        """
        payer, recipient, phone = new_payer(), new_recipient(), new_device()
        third = pix_payload(payer, recipient, T0 + timedelta(minutes=2), device_id=phone)

        registered = post_pix(pix_payload(payer, recipient, T0))
        unseen = post_pix(pix_payload(payer, recipient, T0 + timedelta(minutes=1), device_id=phone))
        seen = post_pix(third)

        assert registered.metadata["device"] == {
            "registration_status": "registered", "registration_source": "demo_seed", "previously_observed": False,
        }
        assert unseen.metadata["device"] == {
            "registration_status": "unregistered", "registration_source": "demo_seed", "previously_observed": False,
        }
        assert seen.metadata["device"] == {
            "registration_status": "unregistered", "registration_source": "demo_seed", "previously_observed": True,
        }
        assert seen.signal_status("P03") == "triggered"
        assert get_pix(third["id"]).content == seen.content
