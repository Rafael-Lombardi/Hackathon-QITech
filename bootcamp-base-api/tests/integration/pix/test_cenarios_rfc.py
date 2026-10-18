"""Os cenários de aceitação da RFC v0.5, pela porta da frente.

Cada teste monta o histórico com POSTs de verdade — datas escolhidas,
nada de INSERT —, manda o Pix do cenário e confere a decisão inteira:
status, linha da tabela, sinais disparados e as duas notas. O docstring
de cada um traz a conta, sinal a sinal, do jeito que a banca refaria no
papel.

Como ler a conta:
    P03 +18        o sinal P03 disparou e somou 18 pontos
    P04 calado     o sinal rodou e não disparou (not_triggered)
    unavailable    o sinal não tinha histórico pra responder, e não soma
    teto 25        o grupo de teto soma no máximo isso
    pagador 46     a nota do eixo, em metadata.scores.payer
    D-ATO-1        a linha da tabela de decisão que casou primeiro

Grupo de teto não é família. A família sai na resposta (Velocity,
Montante, Perfil...); o grupo de teto é o de src/antifraud/policy.py
(CAPS) e junta os sinais que olham o mesmo padrão: o payer_velocity, por
exemplo, junta o P04 (família Velocity) e o P05 (família Montante).

A tabela, em ordem, vale a primeira que casar: restrição ativa → rejeições
combinadas da RFC v0.5 (com evidência externa quando exigida) → D-ATO-1
(P03 e P06) → D-ATO-2 (P03 e P09) → D-PAY (pagador >= 40) → D-RCP
(destino >= 40) → D-OK. Os cenários legados desta classe não semeiam
inteligência externa; por isso, a combinação de R01/R02/R03 não rejeita
sozinha.

O cenário 15 (reenvio e concorrência) mora em test_contrato.py e em
test_concorrencia.py, com os nomes que a RFC deu. As sementes passam pelo
seed (tests/utils/pix.py), que derruba o teste se alguma não voltar
approved. Todos rodam com o DICT na confiança padrão (70): o DICT01 fica
calado aqui, e a camada 3 tem arquivo próprio.
"""

from datetime import timedelta

from tests.utils.clock import T0, TEN_OVER_SEVEN_DAYS, at, days_ago, days_before, minutes_ago, minutes_before, spread_over_days
from tests.utils.identities import new_device, new_ip, new_payer, new_recipient
from tests.utils.pix import pix_payload, post_pix, seed, seed_pix

# Os sinais que comparam o Pix com o histórico. No primeiro Pix de todo
# mundo, todos eles respondem unavailable.
HISTORICAL_SIGNALS = {
    "L02", "G01", "P03", "P04", "P05", "P06", "P07", "P08", "P09", "P10",
    "R02", "R03", "R04", "REL01", "REL02",
}


class TestCenariosRfc:
    def test_primeira_observacao_normal_aprova(self):
        """Cenário 01 — o primeiro Pix na base: contas maduras, R$ 150, às 14h.

        Sem histórico, só a camada 1 tem o que olhar, e ela não acha nada:
        contas abertas há anos, de dia, valor baixo, documentos diferentes.
        Os 14 sinais históricos saem unavailable — "não sei", e não "está
        tudo bem" — e nenhum vira ponto. O DICT responde a confiança
        padrão (70), e o DICT01 fica calado.

        Pagador 0, destino 0, suficiência unavailable nos dois eixos: D-OK.
        Primeira observação não é culpa nem atestado (RFC §14).
        """
        payer, recipient = new_payer(), new_recipient()

        pix = post_pix(pix_payload(payer, recipient, T0, amount=15000))

        assert pix.http_status == 201
        assert pix.status == "approved"
        assert pix.decision_rule == "D-OK"
        assert pix.scores == {"payer": 0, "recipient": 0}
        assert pix.sufficiency == {"payer": "unavailable", "recipient": "unavailable"}
        assert pix.triggered() == set()
        assert pix.signals_with("not_triggered") == {"P01", "L01", "R01", "REL03", "L02", "P03", "DICT01"}
        assert pix.signals_with("unavailable") == (HISTORICAL_SIGNALS - {"L02", "P03"}) | {"DICT02", "DICT03"}

    def test_primeiro_pix_noturno_alto_aprova_com_l01(self):
        """Cenário 02 — o primeiro Pix na base, R$ 5.000 às 3h da manhã.

        L01 +8: 3h está na janela noturna do Banco Central (20h às 6h) e
        R$ 5.000 passa do limite noturno de R$ 1.000. Aqui o limite do BC
        vira indício, e não bloqueio: 8 pontos, longe dos 40 do D-PAY. Os
        sinais históricos seguem unavailable — o P05, que veria o
        montante, ainda não tem com o que comparar.

        Pagador 8, destino 0: D-OK. Não se rejeita só por horário ou valor.
        """
        payer, recipient = new_payer(), new_recipient()

        pix = post_pix(pix_payload(payer, recipient, at(T0, 3), amount=500000))

        assert pix.http_status == 201
        assert pix.status == "approved"
        assert pix.decision_rule == "D-OK"
        assert pix.triggered() == {"L01"}
        assert pix.signal("L01")["points"] == 8
        assert pix.scores == {"payer": 8, "recipient": 0}
        assert pix.signals_with("unavailable") == (HISTORICAL_SIGNALS - {"L02", "P03"}) | {"DICT02", "DICT03"}

    def test_destino_novo_isolado_aprova(self):
        """Cenário 03 — R$ 350 para uma conta aberta há 2 dias, de um pagador regular.

        Preparo: 3 Pix do pagador para o mesmo titular, nos 3 dias
        anteriores, numa conta antiga dele. Pix: R$ 350 para a conta NOVA
        do mesmo titular, aberta há 2 dias.

            R01 +12   a conta de destino tem 2 dias
            destino 12, pagador 0

        Isolado mesmo: o REL01 olha o titular, que o pagador já conhece (3
        aprovados), e não dispara. Só o R01 pesa, e 12 não barra ninguém:
        D-OK.
        """
        payer, recipient = new_payer(), new_recipient()
        seed_pix(payer, recipient, days_before(T0, 3))
        new_account = recipient.with_new_account(opening_date=T0 - timedelta(days=2))

        pix = post_pix(pix_payload(payer, new_account, T0, amount=35000))

        assert pix.http_status == 201
        assert pix.status == "approved"
        assert pix.decision_rule == "D-OK"
        assert pix.triggered() == {"R01"}
        assert pix.signal("R01")["points"] == 12
        assert pix.scores == {"payer": 0, "recipient": 12}

    def test_autotransferencia_aprova(self):
        """Cenário 04 — o pagador manda pra ele mesmo, noutro banco.

        Preparo: 3 Pix do pagador para terceiros, nos 3 dias anteriores.
        Pix: R$ 50 para uma conta dele mesmo (mesmo CPF), noutro banco.

            REL01 +8   o pagador tem histórico e nunca mandou para este
                       documento — o dele
            REL03 -5   pagador e destino são o mesmo titular
            relation: 8 - 5 = 3; destino 3, pagador 0

        D-OK. Autotransferência não é proibida, e também não basta para
        aprovar: o REL03 só desconta.
        """
        payer = new_payer()
        seed_pix(payer, new_recipient(), days_before(T0, 3))
        own_account = new_recipient(document=payer.document)

        pix = post_pix(pix_payload(payer, own_account, T0))

        assert pix.http_status == 201
        assert pix.status == "approved"
        assert pix.decision_rule == "D-OK"
        assert pix.triggered() == {"REL01", "REL03"}
        assert pix.signal("REL01")["points"] == 8
        assert pix.signal("REL03")["points"] == -5
        assert pix.scores == {"payer": 0, "recipient": 3}

    def test_destino_recorrente_reduz_risco(self):
        """Cenário 05 — o mesmo destino há 10 dias, valor de sempre.

        Preparo: 3 Pix para o mesmo destino, há 10, 5 e 1 dia. Pix: R$ 50
        de novo para ele.

            REL02 -8   3 aprovados no par, o primeiro há 10 dias (>= 7), e
                       nenhum 2FA ou rejeição no par na última semana

        O destino fica em 0: a nota do eixo não desce abaixo de zero, e o
        desconto aparece no próprio sinal. Ele passa a pesar quando houver
        o que descontar, e desconta no máximo 10: o piso está em
        test_fronteiras.py, e o desconto segurando um 2FA, em
        test_camada_dict.py. D-OK.
        """
        payer, recipient = new_payer(), new_recipient()
        seed_pix(payer, recipient, days_ago(T0, 10, 5, 1))

        pix = post_pix(pix_payload(payer, recipient, T0))

        assert pix.http_status == 201
        assert pix.status == "approved"
        assert pix.decision_rule == "D-OK"
        assert pix.triggered() == {"REL02"}
        assert pix.signal("REL02")["points"] == -8
        assert pix.scores == {"payer": 0, "recipient": 0}

    def test_device_novo_e_valor_atipico_pede_2fa(self):
        """Cenário 06 — aparelho nunca visto e valor muito acima do padrão: 2FA.

        Preparo: 10 Pix de R$ 1.000 para o mesmo destino, um por dia nos
        10 dias anteriores, sempre do mesmo aparelho. Dá mediana de
        R$ 1.000 com o lastro que o P06 pede (10 aprovados em 7 dias ou
        mais). Pix: R$ 6.200, de um aparelho nunca visto, para um destino
        inédito.

            P03 +18    Dispositivo sem Pix aprovado do pagador
            P06 +18    R$ 6.200 é 6,2x a mediana, e passa de R$ 1.000
            L02 +10    aparelho sem Pix aprovado antes de hoje, acima de R$ 200
            REL01 +8   destino inédito para um pagador com histórico
            pagador 18 + 18 + 10 = 46; destino 8

        O D-ATO-1 casa antes do D-PAY: P03 e P06 juntos é a cara da tomada
        de conta. É o exemplo da RFC §3, número a número. Suficiência:
        pagador medium (10 Pix em 10 dias), destino unavailable (nunca
        recebeu).
        """
        payer, usual, newcomer = new_payer(), new_recipient(), new_recipient()
        seed_pix(payer, usual, days_before(T0, 10), amount=100000)

        pix = post_pix(pix_payload(payer, newcomer, T0, amount=620000, device_id=new_device()))

        assert pix.http_status == 201
        assert pix.status == "two_factor_required"
        assert pix.decision_rule == "D-ATO-1"
        assert pix.triggered() == {"P03", "P06", "L02", "REL01"}
        assert pix.scores == {"payer": 46, "recipient": 8}
        assert pix.sufficiency == {"payer": "medium", "recipient": "unavailable"}
        assert pix.signal("P03")["evidence"]["device_registered"] is False
        assert pix.signal("P03")["evidence"]["device_seen_before"] is False
        assert pix.signal("P06")["evidence"]["median_amount"] == 100000
        assert pix.signal("P06")["evidence"]["ratio"] == 6.2
        assert "6,2" in pix.reasons[0]

    def test_device_novo_valor_baixo_aprova(self):
        """Cenário 07 — aparelho nunca visto, mas R$ 100 para o destino de sempre.

        Preparo: 3 Pix para o mesmo destino, há 8, 4 e 1 dia, do aparelho
        de sempre. Pix: R$ 100 de um aparelho novo, mesmo IP.

            P03 +18     Dispositivo sem Pix aprovado do pagador
            L02 calado  R$ 100 não passa dos R$ 200 por Pix nem dos R$ 1.000
                        do dia
            REL02 -8    destino recorrente (3 aprovados, o primeiro há 8 dias)
            pagador 18, destino 0

        P03 sozinho não pede 2FA: troca de celular acontece. D-OK.
        """
        payer, recipient = new_payer(), new_recipient()
        seed_pix(payer, recipient, days_ago(T0, 8, 4, 1))

        pix = post_pix(pix_payload(payer, recipient, T0, amount=10000, device_id=new_device()))

        assert pix.http_status == 201
        assert pix.status == "approved"
        assert pix.decision_rule == "D-OK"
        assert pix.triggered() == {"P03", "REL02"}
        assert pix.signal_status("L02") == "not_triggered"
        assert pix.scores == {"payer": 18, "recipient": 0}

    def test_quinta_tentativa_em_5min_dispara_p04_e_aprova(self):
        """Cenário 08 — a 5ª tentativa em 5 minutos, valores pequenos.

        Preparo: um Pix para A anteontem e um para B ontem (os dois deixam
        de ser inéditos); depois 4 Pix de R$ 50 nos 4 minutos antes de T0,
        alternando A e B. Pix: o 5º, para A.

            P04 +20    4 anteriores na janela + o atual = 5
            pagador 20, destino 0

        Alternar A e B é o que deixa o P04 sozinho: 5 entradas no mesmo
        destino disparariam o R02, e 3 destinos inéditos, o P07. Abaixo de
        40: D-OK. Rapidez sozinha não é fraude.
        """
        payer, first, second = new_payer(), new_recipient(), new_recipient()
        seed([
            pix_payload(payer, first, T0 - timedelta(days=2)),
            pix_payload(payer, second, T0 - timedelta(days=1)),
        ])
        burst = zip([first, second, first, second], minutes_before(T0, 4))
        seed([pix_payload(payer, recipient, moment) for recipient, moment in burst])

        pix = post_pix(pix_payload(payer, first, T0))

        assert pix.http_status == 201
        assert pix.status == "approved"
        assert pix.decision_rule == "D-OK"
        assert pix.triggered() == {"P04"}
        assert pix.signal("P04")["points"] == 20
        assert pix.signal("P04")["evidence"]["attempts_5m"] == 5
        assert pix.scores == {"payer": 20, "recipient": 0}

    def test_fracionamento_multidestino_pede_2fa(self):
        """Cenário 09 — esvaziar a conta em parcelas, cada uma para um destino inédito.

        Preparo: 3 Pix para um destino de sempre, nos 3 dias anteriores.
        Depois, 4 Pix de R$ 2.000 nos 4 minutos antes de T0, cada um para
        um destino inédito. Pix: o 5º, mais R$ 2.000 para outro destino
        inédito.

            P04 +20    5 tentativas em 5 min, contando o atual
            P05 +15    R$ 10.000 em 1 h, contando o atual (> R$ 8.000)
                       P04 + P05 = 35, teto do payer_velocity: 25
            P07 +18    5 destinos inéditos em 10 min (>= 3)
            REL01 +8   destino inédito
            pagador 25 + 18 = 43; destino 8

        D-PAY (43 >= 40). Repare que o P06 fica unavailable — 3 aprovados,
        longe dos 10 que a mediana pede —, senão R$ 2.000 contra uma
        mediana de R$ 50 dispararia ele também.
        """
        payer = new_payer()
        seed_pix(payer, new_recipient(), days_before(T0, 3))
        seed([pix_payload(payer, new_recipient(), moment, amount=200000) for moment in minutes_before(T0, 4)])

        pix = post_pix(pix_payload(payer, new_recipient(), T0, amount=200000))

        assert pix.http_status == 201
        assert pix.status == "two_factor_required"
        assert pix.decision_rule == "D-PAY"
        assert pix.triggered() == {"P04", "P05", "P07", "REL01"}
        assert pix.signal_status("P06") == "unavailable"
        assert pix.scores == {"payer": 43, "recipient": 8}

    def test_volume_alto_habitual_aprova(self):
        """Cenário 10 — muito Pix, mas tudo do jeito de sempre.

        Preparo: 30 Pix em 10 dias (9h, 13h e 17h), revezando 3 destinos
        de sempre, de R$ 80 a R$ 150, sempre do mesmo aparelho e IP. Pix:
        R$ 150 para um deles, às 14h.

            REL02 -8   destino recorrente
            pagador 0, destino 0

        Nada do pagador dispara: aparelho e IP conhecidos, nenhuma rajada,
        e o P06, que tem lastro de sobra, fica calado — R$ 150 está abaixo
        do piso de R$ 1.000 dele. Suficiência: pagador high (30 Pix em 10
        dias); destino medium (os 10 Pix que ele recebeu deste pagador,
        abaixo dos 30 do high). D-OK. Volume alto não é sinal: o que o
        motor procura é mudança.
        """
        payer = new_payer()
        regulars = [new_recipient(), new_recipient(), new_recipient()]
        amounts = [8000, 12000, 15000]
        moments = spread_over_days([3] * 10, end=T0, every=timedelta(hours=4))
        seed([
            pix_payload(payer, regulars[position % 3], moment, amount=amounts[position % 3])
            for position, moment in enumerate(moments)
        ])

        pix = post_pix(pix_payload(payer, regulars[2], T0, amount=15000))

        assert pix.http_status == 201
        assert pix.status == "approved"
        assert pix.decision_rule == "D-OK"
        assert pix.triggered() == {"REL02"}
        assert pix.signal_status("P06") == "not_triggered"
        assert pix.scores == {"payer": 0, "recipient": 0}
        assert pix.sufficiency == {"payer": "high", "recipient": "medium"}

    def test_destino_maduro_com_muitos_pagadores_aprova(self):
        """Cenário 11 — loja antiga recebendo de 4 clientes em 1 hora.

        Preparo: 3 pagadores diferentes pagam o mesmo destino (conta de
        anos) há 50, 40 e 30 minutos. Pix: um 4º pagador, novo na base.

            R03 +20     4 pagadores distintos em 1 h, contando o atual
            R02 calado  nenhuma entrada nos últimos 10 min
            R01, R04    calados: a conta é antiga
            REL01       unavailable: o pagador nunca foi visto
            destino 20, pagador 0

        D-OK. Negócio legítimo também recebe de muita gente.
        """
        store = new_recipient()
        seed([pix_payload(new_payer(), store, moment) for moment in minutes_ago(T0, 50, 40, 30)])

        pix = post_pix(pix_payload(new_payer(), store, T0))

        assert pix.http_status == 201
        assert pix.status == "approved"
        assert pix.decision_rule == "D-OK"
        assert pix.triggered() == {"R03"}
        assert pix.signal_status("REL01") == "unavailable"
        assert pix.scores == {"payer": 0, "recipient": 20}

    def test_destino_novo_concentrando_pagadores_nao_rejeita_sem_inteligencia(self):
        """Cenário 12 — conta de 2 dias recebendo de 5 pagadores em 5 minutos.

        Preparo: 4 pagadores novos pagam a conta nos 4 minutos antes de
        T0. Pix: um 5º pagador, também novo.

            R01 +12    conta de destino com 2 dias
            R02 +16    5 entradas em 10 min, contando a atual
            R03 +20    5 pagadores distintos em 1 h
            R04 +15    conta nova E muitos pagadores
            12 + 16 + 20 + 15 = 63, teto do recipient_network: 35
            REL01      unavailable: o pagador nunca foi visto
            destino 35, pagador 0

        Sem inteligência externa, R01, R02 e R03 não são suficientes para
        rejeição na RFC v0.5. Como o teto deixa o destino em 35, o Pix é
        aprovado. Repare que a 4ª semente já tinha R01, R03 e R04 (destino
        35) e também foi aprovada: o caso só muda os sinais, não ultrapassa
        o limiar de fricção.
        """
        mule = new_recipient(opening_date=T0 - timedelta(days=2))
        seed([pix_payload(new_payer(), mule, moment) for moment in minutes_before(T0, 4)])

        pix = post_pix(pix_payload(new_payer(), mule, T0))

        assert pix.http_status == 201
        assert pix.status == "approved"
        assert pix.decision_rule == "D-OK"
        assert pix.triggered() == {"R01", "R02", "R03", "R04"}
        assert pix.signal_status("REL01") == "unavailable"
        assert pix.scores == {"payer": 0, "recipient": 35}

    def test_pagador_normal_para_destino_suspeito_pede_2fa_sem_inteligencia(self):
        """Cenário 13 — cliente de sempre mandando para a conta do cenário 12.

        Preparo: o pagador tem 3 Pix nos 3 dias anteriores (aparelho e IP
        conhecidos). A conta de destino é como a do cenário 12: 2 dias, 4
        pagadores novos nos 4 minutos antes de T0. Pix: o cliente paga a
        conta.

            R01, R02, R03, R04   como no 12: recipient_network no teto, 35
            REL01 +8             destino inédito para quem tem histórico
            destino 43, pagador 0

        Sem inteligência externa, a RFC v0.5 não rejeita apenas pelos sinais
        R01/R02/R03. O risco do destino chega a 43 e a política aplica
        D-RCP (2FA). O pagador está limpo, e a confirmação adicional é a
        fricção disponível quando ainda não há evidência suficiente para
        rejeitar definitivamente.
        """
        payer = new_payer()
        seed_pix(payer, new_recipient(), days_before(T0, 3))
        mule = new_recipient(opening_date=T0 - timedelta(days=2))
        seed([pix_payload(new_payer(), mule, moment) for moment in minutes_before(T0, 4)])

        pix = post_pix(pix_payload(payer, mule, T0))

        assert pix.http_status == 201
        assert pix.status == "two_factor_required"
        assert pix.decision_rule == "D-RCP"
        assert pix.triggered() == {"R01", "R02", "R03", "R04", "REL01"}
        assert pix.scores == {"payer": 0, "recipient": 43}

    def test_sem_geolocalizacao_aprova(self):
        """Cenário 14 — o Pix de sempre, sem geolocation.

        Preparo: 3 Pix com geolocalização para o mesmo destino, nos 3 dias
        anteriores. Pix: o mesmo, sem o campo geolocation, que o contrato
        deixa opcional. O G01 fica unavailable sem localização atual; a
        falta do dado não vira indício de nada.

        Nada dispara; pagador 0, destino 0: D-OK.
        """
        payer, recipient = new_payer(), new_recipient()
        seed_pix(payer, recipient, days_before(T0, 3))
        payload = pix_payload(payer, recipient, T0, geolocation=None)
        assert "geolocation" not in payload["source"]

        pix = post_pix(payload)

        assert pix.http_status == 201
        assert pix.status == "approved"
        assert pix.decision_rule == "D-OK"
        assert pix.triggered() == set()
        assert pix.scores == {"payer": 0, "recipient": 0}

    def test_ip_novo_isolado_aprova(self):
        """Cenário 16 — IP nunca visto, do aparelho de sempre, para o destino de sempre.

        Preparo: 3 Pix para o mesmo destino, há 8, 4 e 1 dia. Pix: R$ 50,
        mesmo aparelho, IP novo.

            P09 +6      IP sem Pix aprovado do pagador
            P03 calado  o aparelho é o de sempre (e sem ele não há P10)
            REL02 -8    destino recorrente
            pagador 6, destino 0

        D-OK. IP de celular muda com a rede, e VPN existe: P09 sozinho
        não pede 2FA.
        """
        payer, recipient = new_payer(), new_recipient()
        seed_pix(payer, recipient, days_ago(T0, 8, 4, 1))

        pix = post_pix(pix_payload(payer, recipient, T0, ip=new_ip()))

        assert pix.http_status == 201
        assert pix.status == "approved"
        assert pix.decision_rule == "D-OK"
        assert pix.triggered() == {"P09", "REL02"}
        assert pix.signal("P09")["points"] == 6
        assert pix.signal_status("P03") == "not_triggered"
        assert pix.scores == {"payer": 6, "recipient": 0}

    def test_device_e_ip_novos_pedem_2fa(self):
        """Cenário 17 — aparelho e IP nunca vistos ao mesmo tempo.

        Preparo: 3 Pix para o mesmo destino, nos 3 dias anteriores. Pix:
        R$ 150 para ele, de um aparelho novo, num IP novo.

            P03 +18     Dispositivo sem Pix aprovado do pagador
            P09 +6      IP sem Pix aprovado do pagador
            P10 +12     os dois juntos
            L02 calado  R$ 150 não passa dos R$ 200 por Pix
            pagador 36, destino 0

        D-ATO-2: quem decide é a combinação P03 + P09 — troca do ambiente
        inteiro de acesso —, e não a nota, que ficou nos 36.
        """
        payer, recipient = new_payer(), new_recipient()
        seed_pix(payer, recipient, days_before(T0, 3))

        pix = post_pix(pix_payload(payer, recipient, T0, amount=15000, device_id=new_device(), ip=new_ip()))

        assert pix.http_status == 201
        assert pix.status == "two_factor_required"
        assert pix.decision_rule == "D-ATO-2"
        assert pix.triggered() == {"P03", "P09", "P10"}
        assert pix.signal_status("L02") == "not_triggered"
        assert pix.scores == {"payer": 36, "recipient": 0}

    def test_device_novo_valor_5x_mediana_pede_2fa(self):
        """Cenário 18 — o mesmo golpe do 06, com o lastro mínimo para a mediana.

        Preparo: 10 Pix de R$ 200 para o mesmo destino em EXATAMENTE 7
        dias civis (dois por dia nos três primeiros, um nos outros quatro)
        — o mínimo que destrava o P06. Pix: R$ 1.500 de um aparelho novo,
        mesmo IP, mesmo destino.

            P03 +18    Dispositivo sem Pix aprovado do pagador
            P06 +18    R$ 1.500 é 7,5x a mediana de R$ 200, e passa de R$ 1.000
            L02 +10    aparelho sem Pix aprovado antes de hoje, acima de R$ 200
            REL02 -8   destino recorrente: 10 aprovados, o primeiro há 7 dias
            pagador 46, destino 0

        D-ATO-1. Com 9 Pix, ou com os 10 em 6 dias, o P06 ficaria
        unavailable e o mesmo Pix passaria — essa fronteira está em
        test_fronteiras.py.
        """
        payer, recipient = new_payer(), new_recipient()
        seed_pix(payer, recipient, spread_over_days(TEN_OVER_SEVEN_DAYS), amount=20000)

        pix = post_pix(pix_payload(payer, recipient, T0, amount=150000, device_id=new_device()))

        assert pix.http_status == 201
        assert pix.status == "two_factor_required"
        assert pix.decision_rule == "D-ATO-1"
        assert pix.triggered() == {"P03", "P06", "L02", "REL02"}
        assert pix.signal("P06")["evidence"]["median_amount"] == 20000
        assert pix.signal("P06")["evidence"]["ratio"] == 7.5
        assert pix.scores == {"payer": 46, "recipient": 0}
