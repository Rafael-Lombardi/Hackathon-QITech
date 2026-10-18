"""O G01: a localização do Pix contra os lugares de sempre do pagador.

O sinal só responde com padrão formado: 10 Pix aprovados com
geolocalização, em 7 dias civis ou mais, fora das últimas 24 h (um Pix de
agora não pode ensinar o lugar de agora). Os Pix são agrupados em centros
de 100 km, e só centro com 2 dias ou mais conta; a distância é até o
centro mais perto, e dispara acima de 300 km, com 6 pontos — sinal de
apoio: celular viaja, e sozinho ele não pede 2FA.
"""

from datetime import timedelta

from tests.utils.clock import T0, days_ago
from tests.utils.identities import new_payer, new_recipient
from tests.utils.pix import pix_payload, post_pix, seed, seed_pix

SAO_PAULO = {"latitude": -23.5505, "longitude": -46.6333}
PORTO_ALEGRE = {"latitude": -30.0346, "longitude": -51.2177}  # uns 850 km de São Paulo
CAMPINAS = {"latitude": -22.9068, "longitude": -47.0616}  # uns 85 km de São Paulo

TEN_DAYS = days_ago(T0, 10, 9, 8, 7, 6, 5, 4, 3, 2, 1)


class TestGeolocalizacaoG01:
    def test_historico_insuficiente_deixa_g01_indisponivel(self):
        """Com 9 Pix localizados, não há padrão: G01 unavailable, e a distância não vira indício.

        Preparo: 9 Pix de São Paulo, um por dia nos 9 dias anteriores. Pix:
        de Porto Alegre. Pagador 0, D-OK.
        """
        payer, recipient = new_payer(), new_recipient()
        seed_pix(payer, recipient, TEN_DAYS[1:], geolocation=SAO_PAULO)

        pix = post_pix(pix_payload(payer, recipient, T0, geolocation=PORTO_ALEGRE))

        assert pix.status == "approved"
        assert pix.decision_rule == "D-OK"
        assert pix.signal_status("G01") == "unavailable"
        assert pix.scores["payer"] == 0

    def test_sao_paulo_para_porto_alegre_dispara_mas_nao_decide_sozinho(self):
        """850 km do padrão: G01 +6, e só. Pagador 6, D-OK.

        Preparo: 10 Pix de São Paulo, um por dia nos 10 dias anteriores,
        pro mesmo destino (REL02 -8, no eixo do destino). Pix: de Porto
        Alegre, R$ 50.
        """
        payer, recipient = new_payer(), new_recipient()
        seed_pix(payer, recipient, TEN_DAYS, geolocation=SAO_PAULO)

        pix = post_pix(pix_payload(payer, recipient, T0, geolocation=PORTO_ALEGRE))

        assert pix.status == "approved"
        assert pix.decision_rule == "D-OK"
        assert pix.triggered() == {"G01", "REL02"}
        assert pix.signal("G01")["points"] == 6
        assert pix.signal("G01")["evidence"]["threshold_km"] == 300
        assert pix.scores == {"payer": 6, "recipient": 0}

    def test_dois_centros_habituais_nao_penalizam_viagem_regular(self):
        """Quem vive entre duas cidades tem dois centros, e o Pix de qualquer um deles é padrão.

        Preparo: 5 Pix de São Paulo e 5 de Porto Alegre, um por dia. Pix:
        de Porto Alegre. G01 calado: o centro mais perto está a 0 km.
        """
        payer, recipient = new_payer(), new_recipient()
        seed([
            pix_payload(payer, recipient, moment, geolocation=SAO_PAULO if index < 5 else PORTO_ALEGRE)
            for index, moment in enumerate(TEN_DAYS)
        ])

        pix = post_pix(pix_payload(payer, recipient, T0, geolocation=PORTO_ALEGRE))

        assert pix.status == "approved"
        assert pix.signal_status("G01") == "not_triggered"

    def test_distancia_abaixo_de_300_km_nao_dispara(self):
        """Campinas fica a 85 km de São Paulo: dentro dos 300 km, G01 calado."""
        payer, recipient = new_payer(), new_recipient()
        seed_pix(payer, recipient, TEN_DAYS, geolocation=SAO_PAULO)

        pix = post_pix(pix_payload(payer, recipient, T0, geolocation=CAMPINAS))

        assert pix.status == "approved"
        assert pix.signal_status("G01") == "not_triggered"

    def test_observacoes_das_ultimas_24h_nao_formam_baseline(self):
        """Pix das últimas 24 h não ensinam o lugar de sempre: 10 deles, de 2 em 2 h, e o G01 segue unavailable.

        Sem este corte, o invasor que acabou de tomar a conta ensinaria o
        lugar dele com a própria rajada.
        """
        payer, recipient = new_payer(), new_recipient()
        moments = [T0 - timedelta(hours=23 - index * 2) for index in range(10)]
        seed([pix_payload(payer, recipient, moment, geolocation=SAO_PAULO) for moment in moments])

        pix = post_pix(pix_payload(payer, recipient, T0, geolocation=PORTO_ALEGRE))

        assert pix.status == "approved"
        assert pix.signal_status("G01") == "unavailable"
