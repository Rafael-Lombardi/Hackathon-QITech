"""Cenários reproduzíveis da regra geográfica opcional G01."""

from datetime import timedelta

from tests.utils.clock import T0, days_ago
from tests.utils.identities import new_device, new_payer, new_recipient
from tests.utils.pix import pix_payload, post_pix, seed, seed_pix


SAO_PAULO = {"latitude": -23.5505, "longitude": -46.6333}
PORTO_ALEGRE = {"latitude": -30.0346, "longitude": -51.2177}
CAMPINAS = {"latitude": -22.9068, "longitude": -47.0616}


class TestGeolocalizacaoG01:
    def test_historico_insuficiente_deixa_g01_indisponivel(self):
        payer, recipient = new_payer(), new_recipient()
        seed_pix(payer, recipient, days_ago(T0, 9, 8, 7, 6, 5, 4, 3, 2, 1), geolocation=SAO_PAULO)

        response = post_pix(pix_payload(payer, recipient, T0, geolocation=PORTO_ALEGRE))

        assert response.status == "approved"
        assert response.decision_rule == "D-OK"
        assert response.signal_status("G01") == "unavailable"
        assert response.scores["payer"] == 0

    def test_sao_paulo_para_porto_alegre_dispara_mas_nao_decide_sozinho(self):
        payer, recipient = new_payer(), new_recipient()
        seed_pix(payer, recipient, days_ago(T0, 10, 9, 8, 7, 6, 5, 4, 3, 2, 1), geolocation=SAO_PAULO)

        response = post_pix(pix_payload(payer, recipient, T0, geolocation=PORTO_ALEGRE))

        assert response.status == "approved"
        assert response.decision_rule == "D-OK"
        assert response.signal_status("G01") == "triggered"
        assert response.signal("G01")["points"] == 6
        assert response.scores["payer"] == 6

    def test_dois_centros_habituais_nao_penalizam_viagem_regular(self):
        payer, recipient = new_payer(), new_recipient()
        moments = days_ago(T0, 10, 9, 8, 7, 6, 5, 4, 3, 2, 1)
        history = [
            pix_payload(payer, recipient, moment, geolocation=SAO_PAULO if index < 5 else PORTO_ALEGRE)
            for index, moment in enumerate(moments)
        ]
        seed(history)

        response = post_pix(pix_payload(payer, recipient, T0, geolocation=PORTO_ALEGRE))

        assert response.status == "approved"
        assert response.signal_status("G01") == "not_triggered"

    def test_distancia_abaixo_de_300_km_nao_dispara(self):
        payer, recipient = new_payer(), new_recipient()
        seed_pix(payer, recipient, days_ago(T0, 10, 9, 8, 7, 6, 5, 4, 3, 2, 1), geolocation=SAO_PAULO)

        response = post_pix(pix_payload(payer, recipient, T0, geolocation=CAMPINAS))

        assert response.status == "approved"
        assert response.signal_status("G01") == "not_triggered"

    def test_observacoes_das_ultimas_24h_nao_formam_baseline(self):
        payer, recipient = new_payer(), new_recipient()
        moments = [T0 - timedelta(hours=23 - index * 2) for index in range(10)]
        seed([pix_payload(payer, recipient, moment, geolocation=SAO_PAULO) for moment in moments])

        response = post_pix(pix_payload(payer, recipient, T0, geolocation=PORTO_ALEGRE))

        assert response.status == "approved"
        assert response.signal_status("G01") == "unavailable"

    def test_g01_com_valor_atipico_e_rajada_pede_2fa(self):
        """G01 + P06 + P04 ultrapassam 40, mas sem combinação de rejeição."""
        payer, usual = new_payer(), new_recipient()
        seed_pix(payer, usual, days_ago(T0, 10, 9, 8, 7, 6, 5, 4, 3, 2, 1), amount=100000, geolocation=SAO_PAULO)

        rapid_recipients = [new_recipient() for _ in range(4)]
        seed([
            pix_payload(payer, recipient, T0 - timedelta(minutes=minutes), geolocation=SAO_PAULO)
            for recipient, minutes in zip(rapid_recipients, (4, 3, 2, 1))
        ])
        response = post_pix(pix_payload(payer, new_recipient(), T0, amount=600000, geolocation=PORTO_ALEGRE))

        assert response.status == "two_factor_required"
        assert response.decision_rule == "D-PAY"
        assert response.triggered() >= {"G01", "P04", "P06", "P07"}
        assert response.scores["payer"] >= 40

    def test_g01_com_ato_completo_rejeita(self):
        """G01 + P03 + P04 + P06 + P07 + L02 satisfazem D-REJ-ATO."""
        payer, usual = new_payer(), new_recipient()
        seed_pix(payer, usual, days_ago(T0, 10, 9, 8, 7, 6, 5, 4, 3, 2, 1), amount=100000, geolocation=SAO_PAULO)

        rapid_recipients = [new_recipient() for _ in range(4)]
        seed([
            pix_payload(payer, recipient, T0 - timedelta(minutes=minutes), geolocation=SAO_PAULO)
            for recipient, minutes in zip(rapid_recipients, (4, 3, 2, 1))
        ])
        response = post_pix(
            pix_payload(payer, new_recipient(), T0, amount=600000, device_id=new_device(), geolocation=PORTO_ALEGRE)
        )

        assert response.status == "rejected"
        assert response.decision_rule == "D-REJ-ATO"
        assert response.triggered() >= {"G01", "P03", "P04", "P06", "P07", "L02"}
        assert response.scores["payer"] >= 70
