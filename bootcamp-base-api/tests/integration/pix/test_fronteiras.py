"""As fronteiras: cada limiar testado dos dois lados, mais os tetos e as notas da tabela.

Um par por limiar (RFC §9): o valor que ainda não dispara e o primeiro
que dispara. É aqui que um ">" virando ">=" aparece — o cenário inteiro
ficaria verde, e só a fronteira fica vermelha. Também moram aqui os tetos
e o piso dos grupos, as notas 39/40 do D-PAY e do D-RCP, a pré-condição
de histórico das regras de aparelho e IP e os degraus da suficiência.

Cada teste confere o sinal da fronteira, a nota que ele move e o status.
A conta de cada lado está no docstring; onde o cenário pede histórico, as
sementes passam pelo seed, que confere que cada uma voltou approved.
"""

from dataclasses import replace
from datetime import timedelta

import pytest

from tests.utils.clock import (
    NINE_OVER_SEVEN_DAYS, T0, TEN_OVER_SEVEN_DAYS, TEN_OVER_SIX_DAYS,
    at, days_ago, days_before, minutes_ago, minutes_before, spread_over_days,
)
from tests.utils.identities import new_device, new_ip, new_payer, new_recipient
from tests.utils.pix import pix_payload, post_pix, seed, seed_pix


class TestFronteiras:
    @pytest.mark.parametrize(
        "age_days, expected, payer_score",
        [(6, "triggered", 10), (7, "not_triggered", 0)],
        ids=["6-dias-dispara", "7-dias-nao-dispara"],
    )
    def test_fronteira_p01_idade_da_conta_pagadora(self, age_days, expected, payer_score):
        """P01: conta pagadora com menos de 7 dias. Com 6 dispara (+10); com 7, não.

        O primeiro Pix de um pagador novo: o P01 é da camada 1 e responde
        mesmo sem histórico. Sozinho, fica longe dos 40: D-OK nos dois lados.
        """
        payer = new_payer(opening_date=T0 - timedelta(days=age_days))

        pix = post_pix(pix_payload(payer, new_recipient(), T0))

        assert pix.http_status == 201
        assert pix.signal_status("P01") == expected
        assert pix.scores["payer"] == payer_score
        assert pix.status == "approved"

    def test_p01_usa_a_abertura_do_payload_e_nao_a_da_primeira_aparicao(self):
        """A idade da conta sai da opening_date que veio NESTE Pix.

        A conta nasce no banco com a abertura da primeira aparição (aqui,
        2020) e não muda depois. Quando ela volta com outra abertura — há 2
        dias —, o P01 olha a do payload: a RFC fala na "data de abertura
        informada", e é o que o _account_age_days de
        src/controllers/pix_transaction_controller.py faz.
        """
        payer, recipient = new_payer(), new_recipient()
        seed_pix(payer, recipient, [T0 - timedelta(days=1)])
        reopened = replace(payer, account=replace(payer.account, opening_date=T0 - timedelta(days=2)))

        pix = post_pix(pix_payload(reopened, recipient, T0))

        assert pix.http_status == 201
        assert pix.signal_status("P01") == "triggered"
        assert pix.signal("P01")["evidence"]["account_age_days"] == 2

    def test_p01_conta_aberta_depois_do_pix_tem_idade_0_e_dispara(self):
        """Abertura depois do Pix é dado inconsistente, e a conta conta como nova: idade 0, P01 +10.

        Tratar como nova é o lado conservador, sem inventar um 400 que a
        RFC não prevê (_account_age_days, em
        src/controllers/pix_transaction_controller.py).
        """
        payer = new_payer(opening_date=T0 + timedelta(days=1))

        pix = post_pix(pix_payload(payer, new_recipient(), T0))

        assert pix.http_status == 201
        assert pix.signal_status("P01") == "triggered"
        assert pix.signal("P01")["evidence"]["account_age_days"] == 0

    @pytest.mark.parametrize(
        "hour, minute, expected, payer_score",
        [(19, 59, "not_triggered", 0), (20, 0, "triggered", 8), (5, 59, "triggered", 8), (6, 0, "not_triggered", 0)],
        ids=["19h59-nao-dispara", "20h00-dispara", "05h59-dispara", "06h00-nao-dispara"],
    )
    def test_fronteira_l01_horario_noturno(self, hour, minute, expected, payer_score):
        """L01: a noite do Banco Central vai das 20h às 6h, com as 20h dentro e as 6h fora.

        R$ 1.000,01 nos quatro casos: o valor passa do limite noturno e
        quem decide é só o relógio de São Paulo.
        """
        pix = post_pix(pix_payload(new_payer(), new_recipient(), at(T0, hour, minute), amount=100001))

        assert pix.http_status == 201
        assert pix.signal_status("L01") == expected
        assert pix.scores["payer"] == payer_score
        assert pix.status == "approved"

    @pytest.mark.parametrize(
        "amount, expected, payer_score",
        [(100000, "not_triggered", 0), (100001, "triggered", 8)],
        ids=["1000-reais-nao-dispara", "1000-reais-e-1-centavo-dispara"],
    )
    def test_fronteira_l01_valor(self, amount, expected, payer_score):
        """L01: às 22h, R$ 1.000 em ponto ainda cabe no limite noturno; R$ 1.000,01 não."""
        pix = post_pix(pix_payload(new_payer(), new_recipient(), at(T0, 22), amount=amount))

        assert pix.http_status == 201
        assert pix.signal_status("L01") == expected
        assert pix.scores["payer"] == payer_score

    @pytest.mark.parametrize(
        "transaction_date, expected, local_time",
        [
            ("2026-10-18T01:15:00Z", "triggered", "22:15"),
            ("2026-10-17T22:59:00Z", "not_triggered", None),
            ("2026-10-15T08:00:00Z", "triggered", "05:00"),
        ],
        ids=["01h15Z-vira-22h15-em-sp-dispara", "22h59Z-vira-19h59-em-sp-nao-dispara", "08h00Z-vira-05h00-em-sp-dispara"],
    )
    def test_l01_le_a_hora_de_sao_paulo_mesmo_com_payload_em_utc(self, transaction_date, expected, local_time):
        """O payload chega em UTC e a regra noturna lê a hora de São Paulo (3 h a menos).

        Os dois últimos casos são os que pegam quem lê a hora do payload
        sem converter: 22h59 UTC parece noite e é 19h59 em São Paulo; 8h
        UTC parece dia e é 5h da manhã. R$ 1.500 em todos.
        """
        pix = post_pix(pix_payload(new_payer(), new_recipient(), transaction_date, amount=150000))

        assert pix.http_status == 201
        assert pix.signal_status("L01") == expected
        if local_time:
            assert pix.signal("L01")["evidence"]["local_time"] == local_time

    @pytest.mark.parametrize(
        "amount, expected, payer_score",
        [(20000, "not_triggered", 18), (20001, "triggered", 28)],
        ids=["200-reais-nao-dispara", "200-reais-e-1-centavo-dispara"],
    )
    def test_fronteira_l02_valor_por_pix_em_aparelho_novo(self, amount, expected, payer_score):
        """L02: em aparelho novo, o limite por Pix do Banco Central é R$ 200.

        Preparo: 3 Pix nos 3 dias anteriores, do aparelho de sempre. Pix:
        de um aparelho novo, mesmo IP. O P03 dispara nos dois lados (+18);
        o L02 soma +10 só acima de R$ 200. Sem IP novo e sem mediana (3
        aprovados), nenhum D-ATO casa: pagador 18 ou 28, D-OK.
        """
        payer, recipient = new_payer(), new_recipient()
        seed_pix(payer, recipient, days_before(T0, 3))

        pix = post_pix(pix_payload(payer, recipient, T0, amount=amount, device_id=new_device()))

        assert pix.http_status == 201
        assert pix.signal_status("P03") == "triggered"
        assert pix.signal_status("L02") == expected
        assert pix.scores["payer"] == payer_score
        assert pix.status == "approved"

    def test_fronteira_l02_soma_do_dia_dispara_acima_de_1000(self):
        """L02: em aparelho novo, o limite do DIA é R$ 1.000 — e conta o 2º, o 3º Pix.

        Preparo: 3 Pix nos 3 dias anteriores, do aparelho de sempre. Hoje,
        de um aparelho novo: 5 Pix de R$ 200, de meia em meia hora.

            1º Pix    P03 +18 (Dispositivo sem Pix aprovado do pagador), e
                      aprovado: daí em diante o aparelho tem Pix aprovado,
                      mas só de HOJE
            5º Pix    soma do dia 5 x R$ 200 = R$ 1.000 → L02 calado
            6º Pix    R$ 0,01 a mais: R$ 1.000,01 → L02 +10
            amanhã    R$ 500 no mesmo aparelho, agora aprovado ontem → calado

        É o ajuste da v0.5 ao L02 da RFC: com "aparelho nunca visto" ao
        pé da letra, a soma do dia seria sempre o próprio Pix.
        """
        payer, recipient = new_payer(), new_recipient()
        seed_pix(payer, recipient, days_before(T0, 3))
        new_phone = new_device()
        morning = [at(T0, 9) + timedelta(minutes=30 * step) for step in range(5)]

        today = seed_pix(payer, recipient, morning, amount=20000, device_id=new_phone)
        over = post_pix(pix_payload(payer, recipient, at(T0, 11, 30), amount=1, device_id=new_phone))
        tomorrow = post_pix(pix_payload(payer, recipient, at(T0, 10) + timedelta(days=1), amount=50000, device_id=new_phone))

        assert today[0].signal_status("P03") == "triggered"
        assert today[-1].signal_status("L02") == "not_triggered"
        assert over.http_status == 201
        assert over.signal_status("P03") == "not_triggered"
        assert over.signal_status("L02") == "triggered"
        assert tomorrow.http_status == 201
        assert tomorrow.signal_status("L02") == "not_triggered"

    @pytest.mark.parametrize(
        "earlier_days, expected, payer_score, status",
        [(2, "unavailable", 0, "approved"), (3, "triggered", 46, "two_factor_required")],
        ids=["2-pix-aprovados-unavailable", "3-pix-aprovados-dispara"],
    )
    def test_l02_p03_p09_p10_pedem_3_pix_aprovados(self, earlier_days, expected, payer_score, status):
        """Aparelho e IP inéditos só dizem algo depois de 3 Pix aprovados do pagador.

        O mesmo Pix — R$ 250, de aparelho e IP novos — depois de 2 ou de 3
        Pix aprovados nos dias anteriores. Pra quem acabou de chegar, quase
        tudo é inédito, e com 2 os quatro sinais saem unavailable: D-OK.
        Contam só os aprovados — o porquê está em test_historico.py. Com 3:

            P03 +18    Dispositivo sem Pix aprovado do pagador
            P09 +6     IP sem Pix aprovado do pagador
            P10 +12    os dois juntos
            L02 +10    aparelho sem Pix aprovado antes de hoje, acima de R$ 200
            pagador 36 (payer_behavior) + 10 (limit) = 46

        D-ATO-2 (P03 e P09).
        """
        payer, recipient = new_payer(), new_recipient()
        seed_pix(payer, recipient, days_before(T0, earlier_days))

        pix = post_pix(pix_payload(payer, recipient, T0, amount=25000, device_id=new_device(), ip=new_ip()))

        assert pix.http_status == 201
        assert {pix.signal_status(code) for code in ("L02", "P03", "P09", "P10")} == {expected}
        assert pix.scores["payer"] == payer_score
        assert pix.status == status

    @pytest.mark.parametrize(
        "amount, expected, payer_score, status, decision_rule",
        [(499999, "not_triggered", 0, "approved", "D-OK"), (500000, "triggered", 15, "two_factor_required", "D-LIMIT")],
        ids=["4999-reais-e-99-nao-dispara", "5000-reais-dispara"],
    )
    def test_fronteira_l03_teto_de_quem_nunca_teve_pix_aprovado(self, amount, expected, payer_score, status, decision_rule):
        """L03: quem nunca teve Pix aprovado nesta base tem teto de R$ 5.000 por Pix — e o teto já conta.

        O primeiro Pix de um cliente novo, às 14h, pra uma conta antiga:
        R$ 4.999,99 passa sem sinal nenhum; R$ 5.000 dispara o L03 (+15),
        e a linha do teto pede 2FA seja qual for a nota — D-LIMIT. É o
        limite inicial baixo da conta recém-aberta, aqui pra quem a base
        ainda não viu aprovar nada.
        """
        pix = post_pix(pix_payload(new_payer(), new_recipient(), T0, amount=amount))

        assert pix.http_status == 201
        assert pix.signal_status("L03") == expected
        assert pix.scores["payer"] == payer_score
        assert pix.status == status
        assert pix.decision_rule == decision_rule

    @pytest.mark.parametrize(
        "amount, expected, payer_score, status, decision_rule",
        [(4999999, "not_triggered", 15, "approved", "D-OK"), (5000000, "triggered", 30, "two_factor_required", "D-LIMIT")],
        ids=["49999-reais-e-99-nao-dispara", "50000-reais-dispara"],
    )
    def test_fronteira_l03_teto_por_pix(self, amount, expected, payer_score, status, decision_rule):
        """L03: com Pix aprovado na base, o teto por Pix é R$ 50.000.

        Preparo: 3 Pix nos 3 dias anteriores. Pix: R$ 49.999,99 ou
        R$ 50.000, pro destino de sempre. O P05 dispara nos dois lados
        (+15: mais de R$ 8.000 em 1 h); o L03 só no segundo (+15, no grupo
        dele): pagador 15 ou 30, e a linha do teto decide antes da nota.
        """
        payer, recipient = new_payer(), new_recipient()
        seed_pix(payer, recipient, days_before(T0, 3))

        pix = post_pix(pix_payload(payer, recipient, T0, amount=amount))

        assert pix.http_status == 201
        assert pix.signal_status("L03") == expected
        assert pix.signal_status("P05") == "triggered"
        assert pix.scores["payer"] == payer_score
        assert pix.status == status
        assert pix.decision_rule == decision_rule

    def test_l03_nao_olha_pix_para_o_proprio_titular(self):
        """Pra si mesmo não há teto: R$ 50.000 pra outra conta do mesmo CPF passa sem L03.

        Preparo: 3 Pix nos 3 dias anteriores, pra terceiros. Pix:
        R$ 50.000 pra uma conta do próprio pagador, noutro banco.

            P05 +15     mais de R$ 8.000 em 1 h
            REL01 +8    destino inédito (o dele mesmo)
            REL03 -5    mesmo titular
            pagador 15; destino 3

        D-OK. O teto existe pra segurar dinheiro saindo pra terceiro.
        """
        payer = new_payer()
        seed_pix(payer, new_recipient(), days_before(T0, 3))
        own_account = new_recipient(document=payer.document)

        pix = post_pix(pix_payload(payer, own_account, T0, amount=5000000))

        assert pix.http_status == 201
        assert pix.signal_status("L03") == "not_triggered"
        assert pix.triggered() == {"P05", "REL01", "REL03"}
        assert pix.scores == {"payer": 15, "recipient": 3}
        assert pix.status == "approved"

    @pytest.mark.parametrize(
        "earlier_minutes, expected",
        [((3, 2, 1), "not_triggered"), ((4, 3, 2, 1), "triggered")],
        ids=["4a-tentativa-nao-dispara", "5a-tentativa-dispara"],
    )
    def test_fronteira_p04_tentativas_em_5_min(self, earlier_minutes, expected):
        """P04: 5 tentativas do pagador em 5 minutos, contando a atual.

        Preparo: 3 ou 4 Pix nos minutos antes de T0. Pix: o 4º ou o 5º.
        """
        payer, recipient = new_payer(), new_recipient()
        seed_pix(payer, recipient, minutes_ago(T0, *earlier_minutes))

        pix = post_pix(pix_payload(payer, recipient, T0))

        assert pix.http_status == 201
        assert pix.signal_status("P04") == expected
        assert pix.status == "approved"

    def test_p04_janela_nao_conta_o_pix_de_exatamente_5_min_atras(self):
        """A janela de 5 min é aberta no começo: (t - 5 min, t].

        Preparo: Pix há 5, 3, 2 e 1 minuto. O de 5 minutos atrás fica de
        fora, e o atual é a 4ª tentativa, não a 5ª: P04 calado.
        """
        payer, recipient = new_payer(), new_recipient()
        seed_pix(payer, recipient, minutes_ago(T0, 5, 3, 2, 1))

        pix = post_pix(pix_payload(payer, recipient, T0))

        assert pix.http_status == 201
        assert pix.signal_status("P04") == "not_triggered"

    @pytest.mark.parametrize(
        "amount, expected, payer_score",
        [(400000, "not_triggered", 0), (400001, "triggered", 15)],
        ids=["8000-reais-nao-dispara", "8000-reais-e-1-centavo-dispara"],
    )
    def test_fronteira_p05_montante_em_1_hora(self, amount, expected, payer_score):
        """P05: mais de R$ 8.000 em 1 hora, contando o atual.

        Preparo: R$ 4.000 há 30 minutos. Pix: R$ 4.000 (soma R$ 8.000, no
        limite) ou R$ 4.000,01 (passa). Sozinho, +15: D-OK.
        """
        payer, recipient = new_payer(), new_recipient()
        seed_pix(payer, recipient, [T0 - timedelta(minutes=30)], amount=400000)

        pix = post_pix(pix_payload(payer, recipient, T0, amount=amount))

        assert pix.http_status == 201
        assert pix.signal_status("P05") == expected
        assert pix.scores["payer"] == payer_score
        assert pix.status == "approved"

    @pytest.mark.parametrize(
        "amount, expected, payer_score",
        [(100000, "not_triggered", 0), (100200, "triggered", 18)],
        ids=["5x-a-mediana-nao-dispara", "5-01x-a-mediana-dispara"],
    )
    def test_fronteira_p06_mais_de_5x_a_mediana(self, amount, expected, payer_score):
        """P06: valor MAIS de 5 vezes a mediana. 5,0x em ponto não dispara; 5,01x dispara.

        Preparo: 10 Pix de R$ 200 em 7 dias, do aparelho de sempre —
        mediana de R$ 200, com o lastro que o P06 pede. Pix: R$ 1.000 (5,0x)
        ou R$ 1.002 (5,01x). Aparelho conhecido, nenhum D-ATO: P06 sozinho,
        +18, D-OK.
        """
        payer, recipient = new_payer(), new_recipient()
        seed_pix(payer, recipient, spread_over_days(TEN_OVER_SEVEN_DAYS), amount=20000)

        pix = post_pix(pix_payload(payer, recipient, T0, amount=amount))

        assert pix.http_status == 201
        assert pix.signal_status("P06") == expected
        assert pix.scores["payer"] == payer_score
        assert pix.status == "approved"

    @pytest.mark.parametrize(
        "per_day, expected",
        [
            (NINE_OVER_SEVEN_DAYS, "unavailable"),
            (TEN_OVER_SIX_DAYS, "unavailable"),
            (TEN_OVER_SEVEN_DAYS, "triggered"),
        ],
        ids=["9-aprovados-em-7-dias-unavailable", "10-aprovados-em-6-dias-unavailable", "10-aprovados-em-7-dias-dispara"],
    )
    def test_fronteira_p06_pre_condicao_10_aprovados_em_7_dias(self, per_day, expected):
        """P06 só compara com a mediana quando há lastro: 10 aprovados em 7 dias civis.

        O mesmo Pix — R$ 1.500, 7,5x a mediana de R$ 200, do aparelho de
        sempre — nos três históricos. Com 9 Pix, ou com os 10 espalhados
        em só 6 dias, a mediana não vale nada e o P06 diz "não sei"
        (unavailable, sem ponto). Com 10 em 7 dias, dispara (+18). Os três
        terminam aprovados.
        """
        payer, recipient = new_payer(), new_recipient()
        seed_pix(payer, recipient, spread_over_days(per_day), amount=20000)

        pix = post_pix(pix_payload(payer, recipient, T0, amount=150000))

        assert pix.http_status == 201
        assert pix.signal_status("P06") == expected
        assert pix.status == "approved"

    @pytest.mark.parametrize(
        "earlier_minutes, expected, payer_score",
        [((2,), "not_triggered", 0), ((4, 2), "triggered", 18)],
        ids=["2-destinos-ineditos-nao-dispara", "3-destinos-ineditos-dispara"],
    )
    def test_fronteira_p07_destinos_ineditos_em_10_min(self, earlier_minutes, expected, payer_score):
        """P07: 3 destinos inéditos em 10 minutos, contando o atual.

        Preparo: o pagador tem 1 Pix de ontem, para outro destino. Nos
        minutos antes de T0, 1 ou 2 Pix, cada um para um destino inédito.
        Pix: mais um destino inédito — o 2º ou o 3º da janela. O REL01
        (+8, destino) dispara nos dois lados; P07 sozinho fica nos 18: D-OK.
        """
        payer = new_payer()
        seed_pix(payer, new_recipient(), [T0 - timedelta(days=1)])
        seed([pix_payload(payer, new_recipient(), moment) for moment in minutes_ago(T0, *earlier_minutes)])

        pix = post_pix(pix_payload(payer, new_recipient(), T0))

        assert pix.http_status == 201
        assert pix.signal_status("P07") == expected
        assert pix.scores["payer"] == payer_score
        assert pix.status == "approved"

    def test_fronteira_p08_dispara_no_terceiro_pagador_do_aparelho(self):
        """P08: o mesmo aparelho pagando por 3 documentos em 24 h, contando o atual.

        Três pagadores novos usam o mesmo aparelho, há 2 h, há 1 h e
        agora. O 1º encontra o aparelho sem histórico (unavailable); o 2º
        vê 2 pagadores (calado); o 3º vê 3 e dispara (+12). É o primeiro
        Pix de cada um deles, então o P03 fica unavailable e a nota do 3º
        é só o P08: 12. Sinal de apoio, sozinho não pede 2FA.
        """
        shared = new_device()
        first, second = seed([
            pix_payload(new_payer(), new_recipient(), T0 - timedelta(hours=hours), device_id=shared)
            for hours in (2, 1)
        ])

        third = post_pix(pix_payload(new_payer(), new_recipient(), T0, device_id=shared))

        assert first.signal_status("P08") == "unavailable"
        assert second.signal_status("P08") == "not_triggered"
        assert third.http_status == 201
        assert third.signal_status("P08") == "triggered"
        assert third.scores["payer"] == 12
        assert third.status == "approved"

    @pytest.mark.parametrize(
        "earlier_minutes, expected, recipient_score",
        [((7, 5, 3), "not_triggered", 0), ((9, 7, 5, 3), "triggered", 16)],
        ids=["4a-entrada-nao-dispara", "5a-entrada-dispara"],
    )
    def test_fronteira_r02_entradas_em_10_min(self, earlier_minutes, expected, recipient_score):
        """R02: 5 entradas no mesmo TITULAR em 10 minutos, mesmo em contas diferentes.

        Preparo: o mesmo pagador manda 3 ou 4 Pix de 2 em 2 minutos,
        alternando duas contas do mesmo titular. Pix: a 4ª ou a 5ª entrada.
        Com um único pagador, o R03 fica de fora, e o espaço de 2 minutos
        deixa o P04 de fora (no máximo 2 na janela de 5 min). Sozinho,
        +16: D-OK.
        """
        payer, holder = new_payer(), new_recipient()
        accounts = [holder, holder.with_new_account()]
        moments = minutes_ago(T0, *earlier_minutes)
        seed([pix_payload(payer, accounts[position % 2], moment) for position, moment in enumerate(moments)])

        pix = post_pix(pix_payload(payer, accounts[0], T0))

        assert pix.http_status == 201
        assert pix.signal_status("R02") == expected
        assert pix.signal_status("R03") == "not_triggered"
        assert pix.scores["recipient"] == recipient_score
        assert pix.status == "approved"

    @pytest.mark.parametrize(
        "earlier_minutes, expected, recipient_score",
        [((40, 30), "not_triggered", 0), ((50, 40, 30), "triggered", 20)],
        ids=["3-pagadores-nao-dispara", "4-pagadores-dispara"],
    )
    def test_fronteira_r03_pagadores_distintos_em_1_hora(self, earlier_minutes, expected, recipient_score):
        """R03: 4 pagadores distintos para o mesmo TITULAR em 1 hora, contando o atual.

        Preparo: 2 ou 3 pagadores novos pagam o titular nos últimos 50
        minutos, alternando duas contas dele. Pix: mais um pagador novo.
        Destino de conta antiga: sem R01, sem R04. Sozinho, +20: D-OK.
        """
        holder = new_recipient()
        accounts = [holder, holder.with_new_account()]
        moments = minutes_ago(T0, *earlier_minutes)
        seed([pix_payload(new_payer(), accounts[position % 2], moment) for position, moment in enumerate(moments)])

        pix = post_pix(pix_payload(new_payer(), accounts[1], T0))

        assert pix.http_status == 201
        assert pix.signal_status("R03") == expected
        assert pix.scores["recipient"] == recipient_score
        assert pix.status == "approved"

    @pytest.mark.parametrize(
        "earlier_days, expected, points",
        [((7, 4, 1), "triggered", -8), ((6, 4, 1), "not_triggered", 0), ((7, 4), "not_triggered", 0)],
        ids=["3-aprovados-o-primeiro-ha-7-dias-dispara", "o-primeiro-ha-6-dias-nao-dispara", "so-2-aprovados-nao-dispara"],
    )
    def test_fronteira_rel02_relacao_recorrente(self, earlier_days, expected, points):
        """REL02: 3 Pix aprovados no par, o primeiro há 7 dias ou mais.

        Preparo: Pix para o mesmo destino nos dias indicados. Com 3, o
        primeiro há exatamente 7 dias (de 24 h), desconta 8; o primeiro há
        6 dias, ou só 2 Pix, não. A nota do destino fica em 0 nos três —
        o eixo não desce abaixo de zero —, e o desconto aparece no sinal.
        """
        payer, recipient = new_payer(), new_recipient()
        seed_pix(payer, recipient, days_ago(T0, *earlier_days))

        pix = post_pix(pix_payload(payer, recipient, T0))

        assert pix.http_status == 201
        assert pix.signal_status("REL02") == expected
        assert pix.signal("REL02")["points"] == points
        assert pix.status == "approved"

    def test_teto_do_payer_velocity_segura_p04_mais_p05_em_25(self):
        """P04 e P05 são a mesma rajada medida de dois jeitos, e o payer_velocity soma no máximo 25.

        Preparo: um Pix para A anteontem e um para B ontem; depois 4 Pix de
        R$ 2.000 nos 4 minutos antes de T0, alternando A e B. Pix: R$ 2.000
        para A.

            P04 +20    5 tentativas em 5 min
            P05 +15    R$ 10.000 em 1 h
            20 + 15 = 35, teto do payer_velocity: 25
            pagador 25

        O teto mora na nota, e não no sinal: cada um mostra os pontos
        dele. D-OK.
        """
        payer, first, second = new_payer(), new_recipient(), new_recipient()
        seed([
            pix_payload(payer, first, T0 - timedelta(days=2)),
            pix_payload(payer, second, T0 - timedelta(days=1)),
        ])
        burst = zip([first, second, first, second], minutes_before(T0, 4))
        seed([pix_payload(payer, recipient, moment, amount=200000) for recipient, moment in burst])

        pix = post_pix(pix_payload(payer, first, T0, amount=200000))

        assert pix.http_status == 201
        assert pix.triggered() == {"P04", "P05"}
        assert pix.signal("P04")["points"] == 20
        assert pix.signal("P05")["points"] == 15
        assert pix.scores == {"payer": 25, "recipient": 0}
        assert pix.decision_rule == "D-OK"

    def test_teto_do_payer_behavior_segura_em_45(self):
        """Os sinais de comportamento do pagador somam no máximo 45.

        Preparo: dois pagadores novos usam um aparelho há 2 h e há 1 h. O
        pagador do teste tem 3 Pix nos dias anteriores, do aparelho dele.
        Pix: R$ 50 desse aparelho emprestado, num IP novo.

            P03 +18    Dispositivo sem Pix aprovado do pagador
            P08 +12    o aparelho já pagou por 3 documentos em 24 h
            P09 +6     IP sem Pix aprovado do pagador
            P10 +12    aparelho e IP juntos
            18 + 12 + 6 + 12 = 48, teto do payer_behavior: 45
            pagador 45

        D-ATO-2 (P03 e P09).
        """
        shared = new_device()
        seed([pix_payload(new_payer(), new_recipient(), T0 - timedelta(hours=hours), device_id=shared) for hours in (2, 1)])
        payer, recipient = new_payer(), new_recipient()
        seed_pix(payer, recipient, days_before(T0, 3))

        pix = post_pix(pix_payload(payer, recipient, T0, device_id=shared, ip=new_ip()))

        assert pix.http_status == 201
        assert pix.triggered() == {"P03", "P08", "P09", "P10"}
        assert sum(pix.signal(code)["points"] for code in ("P03", "P08", "P09", "P10")) == 48
        assert pix.scores["payer"] == 45
        assert pix.decision_rule == "D-ATO-2"

    def test_piso_da_relation_segura_em_menos_10(self):
        """A relação desconta no máximo 10: histórico bom não apaga indício.

        Preparo: o pagador manda 3 Pix pra outra conta DELE, há 10, 5 e 1
        dia. Pix: pra uma terceira conta dele, aberta há 2 dias.

            R01 +12    a conta de destino tem 2 dias
            REL02 -8   relação recorrente consigo mesmo
            REL03 -5   mesmo titular
            -8 - 5 = -13, piso da relation: -10
            destino 12 - 10 = 2

        Sem o piso, seriam 12 - 13 = -1, e a nota pararia em 0. D-OK.
        """
        payer = new_payer()
        own_old = new_recipient(document=payer.document)
        own_new = own_old.with_new_account(opening_date=T0 - timedelta(days=2))
        seed_pix(payer, own_old, days_ago(T0, 10, 5, 1))

        pix = post_pix(pix_payload(payer, own_new, T0))

        assert pix.http_status == 201
        assert pix.triggered() == {"R01", "REL02", "REL03"}
        assert pix.scores["recipient"] == 2
        assert pix.decision_rule == "D-OK"

    def test_fronteira_d_pay_39_aprova(self):
        """O D-PAY pede pagador >= 40. Com 39, aprova.

        Preparo: conta aberta há 2 dias; 3 Pix às 10h (anteontem, ontem e
        hoje), do aparelho de sempre. Pix: R$ 8.100 às 21h, num IP novo.

            P01 +10    conta pagadora com 2 dias
            L01 +8     21h, acima de R$ 1.000
            P05 +15    R$ 8.100 em 1 h (> R$ 8.000)
            P09 +6     IP sem Pix aprovado do pagador (o aparelho é o de
                       sempre: sem P03, sem D-ATO-2)
            pagador 10 + 8 + 15 + 6 = 39

        D-OK. O outro lado da fronteira é o teste seguinte.
        """
        payer = new_payer(opening_date=at(T0, 9) - timedelta(days=2))
        recipient = new_recipient()
        seed_pix(payer, recipient, days_before(at(T0, 10), 2) + [at(T0, 10)])

        pix = post_pix(pix_payload(payer, recipient, at(T0, 21), amount=810000, ip=new_ip()))

        assert pix.http_status == 201
        assert pix.triggered() == {"P01", "L01", "P05", "P09"}
        assert pix.scores["payer"] == 39
        assert pix.status == "approved"
        assert pix.decision_rule == "D-OK"

    def test_fronteira_d_pay_40_pede_2fa(self):
        """O D-PAY pede pagador >= 40. Com 40, 2FA.

        A mesma história do teste anterior, com uma peça trocada: o Pix de
        hoje às 10h saiu de um aparelho novo, e o das 21h sai dele, no IP
        de sempre. O aparelho já tem um Pix aprovado hoje — P03 calado —,
        mas nenhum de antes de hoje, e é isso que o L02 olha.

            P01 +10    conta pagadora com 2 dias
            L01 +8     21h, acima de R$ 1.000
            L02 +10    aparelho sem Pix aprovado antes de hoje, acima de R$ 200
                       L01 + L02 = 18, teto do limit: 15
            P05 +15    R$ 8.100 em 1 h
            pagador 10 + 15 + 15 = 40

        D-PAY: 2FA.
        """
        payer = new_payer(opening_date=at(T0, 9) - timedelta(days=2))
        recipient = new_recipient()
        new_phone = new_device()
        seed_pix(payer, recipient, days_before(at(T0, 10), 2))
        seed_pix(payer, recipient, [at(T0, 10)], device_id=new_phone)

        pix = post_pix(pix_payload(payer, recipient, at(T0, 21), amount=810000, device_id=new_phone))

        assert pix.http_status == 201
        assert pix.triggered() == {"P01", "L01", "L02", "P05"}
        assert pix.scores["payer"] == 40
        assert pix.status == "two_factor_required"
        assert pix.decision_rule == "D-PAY"

    def test_fronteira_d_rcp_39_aprova(self, dict_mock):
        """O D-RCP pede destino >= 40. Com 39, aprova.

        O destino soma quatro grupos: o recipient_network (R01 12, R02 16,
        R03 20, R04 15; teto 40), a estatística DICT (DICT01 32; teto 32),
        as marcações do DICT (DICT02 20, DICT03 25; teto 25) e a relation
        (REL01 8, REL02 -8, REL03 -5; de -10 a 8). O 39 é este:

        Um cliente novo manda R$ 50 pra uma conta DELE, noutro banco, aberta
        há 2 dias, e o DICT dá confiança 12 pro documento dele.

            R01 +12      a conta de destino tem 2 dias
            DICT01 +32   confiança 12 (< 30)
            REL03 -5     mesmo titular
            REL01        unavailable: o pagador nunca foi visto
            destino 12 + 32 - 5 = 39

        D-OK. O outro lado da fronteira é o teste seguinte.
        """
        payer = new_payer()
        own_new = new_recipient(document=payer.document, opening_date=T0 - timedelta(days=2))
        dict_mock.set_security_statistics(payer.document, 12)

        pix = post_pix(pix_payload(payer, own_new, T0))

        assert pix.http_status == 201
        assert pix.triggered() == {"R01", "DICT01", "REL03"}
        assert pix.scores["recipient"] == 39
        assert pix.status == "approved"
        assert pix.decision_rule == "D-OK"

    def test_fronteira_d_rcp_40_pede_2fa(self, dict_mock):
        """O D-RCP pede destino >= 40. Com 40, 2FA: o DICT01 somado ao REL01.

        Preparo: o pagador tem 1 Pix de ontem, para outro destino. Pix: para
        um destino que ele nunca pagou, com confiança 12 no DICT.

            DICT01 +32   confiança 12 (< 30)
            REL01 +8     destino inédito para quem tem histórico
            destino 40

        D-RCP. A nota de fora, sozinha, fica nos 32 e não pede 2FA; com
        qualquer indício daqui sobre o destino, pede.
        """
        payer, recipient = new_payer(), new_recipient()
        seed_pix(payer, new_recipient(), [T0 - timedelta(days=1)])
        dict_mock.set_security_statistics(recipient.document, 12)

        pix = post_pix(pix_payload(payer, recipient, T0))

        assert pix.http_status == 201
        assert pix.triggered() == {"DICT01", "REL01"}
        assert pix.scores == {"payer": 0, "recipient": 40}
        assert pix.status == "two_factor_required"
        assert pix.decision_rule == "D-RCP"

    @pytest.mark.parametrize(
        "destination, status, decision_rule, triggered, recipient_score",
        [
            ("inedito", "two_factor_required", "D-SCAM", {"P06", "REL01"}, 8),
            ("habitual", "approved", "D-OK", {"P06", "REL02"}, 0),
            ("proprio", "approved", "D-OK", {"P06", "REL01", "REL03"}, 3),
        ],
        ids=["destino-inedito-pede-2fa", "destino-habitual-aprova", "propria-conta-aprova"],
    )
    def test_d_scam_valor_atipico_para_destino_inedito(self, destination, status, decision_rule, triggered, recipient_score):
        """D-SCAM: valor muito acima do padrão pra um destino que o pagador nunca pagou pede 2FA, mesmo com a nota baixa.

        É a falsa central, o falso parente e o sequestro relâmpago: o
        cliente, do próprio aparelho, manda um valor que nunca mandou pra
        alguém que nunca pagou. Preparo: 10 Pix de R$ 400 pro destino de
        sempre, em 7 dias (mediana R$ 400). Pix: R$ 3.500, 8,75x a mediana.

            destino inédito    P06 +18, REL01 +8: pagador 18, destino 8 → D-SCAM
            destino habitual   P06 +18, REL02 -8: pagador 18, destino 0 → D-OK
            conta própria      P06 +18, REL01 +8, REL03 -5: destino 3 → D-OK

        Com 18 pontos o D-PAY não casa; é a combinação que decide. A
        autotransferência fica de fora (REL03): pra si mesmo, destino
        inédito não é inédito.
        """
        payer, usual = new_payer(), new_recipient()
        seed_pix(payer, usual, spread_over_days(TEN_OVER_SEVEN_DAYS), amount=40000)
        recipients = {"inedito": new_recipient(), "habitual": usual, "proprio": new_recipient(document=payer.document)}

        pix = post_pix(pix_payload(payer, recipients[destination], T0, amount=350000))

        assert pix.http_status == 201
        assert pix.triggered() == triggered
        assert pix.scores == {"payer": 18, "recipient": recipient_score}
        assert pix.status == status
        assert pix.decision_rule == decision_rule
        if decision_rule == "D-SCAM":
            assert pix.reasons == ["Valor 8,75x a mediana para um destino que o pagador nunca pagou"]

    @pytest.mark.parametrize(
        "same_destination, status, decision_rule, triggered, scores",
        [
            (False, "two_factor_required", "D-DRAIN", {"P04", "P07", "REL01"}, {"payer": 38, "recipient": 8}),
            (True, "approved", "D-OK", {"P04", "R02"}, {"payer": 20, "recipient": 16}),
        ],
        ids=["destinos-ineditos-pede-2fa", "mesmo-destino-aprova"],
    )
    def test_d_drain_rajada_para_destinos_ineditos(self, same_destination, status, decision_rule, triggered, scores):
        """D-DRAIN: 5 tentativas em 5 min com 3 ou mais destinos inéditos pede 2FA antes de a nota chegar aos 40.

        É o dreno da conta tomada em parcelas pequenas. Preparo: 3 Pix nos
        3 dias anteriores, pro destino de sempre. Nos 4 minutos antes de
        T0, 4 Pix de R$ 50. Pix: o 5º.

            destinos inéditos    P04 +20, P07 +18, REL01 +8: pagador 38,
                                 destino 8 → D-DRAIN (38 < 40: o D-PAY não
                                 casa, a combinação casa)
            o destino de sempre  P04 +20 e, do lado do destino, R02 +16 (5
                                 entradas em 10 min): pagador 20, destino
                                 16 → D-OK. Rapidez pro mesmo destino não
                                 é dreno.
        """
        payer, usual = new_payer(), new_recipient()
        seed_pix(payer, usual, days_before(T0, 3))

        def destination():
            return usual if same_destination else new_recipient()

        seed([pix_payload(payer, destination(), moment) for moment in minutes_before(T0, 4)])

        pix = post_pix(pix_payload(payer, destination(), T0))

        assert pix.http_status == 201
        assert pix.triggered() == triggered
        assert pix.scores == scores
        assert pix.status == status
        assert pix.decision_rule == decision_rule
        if decision_rule == "D-DRAIN":
            assert pix.reasons == ["5 tentativas em 5 min e 5 destinos inéditos em 10 min"]

    def test_suficiencia_sobe_de_unavailable_a_medium(self):
        """Suficiência, os primeiros degraus: 0 Pix, unavailable; 1 a 5, low; 6 em 2 dias ou mais, medium.

        Um pagador manda 5 Pix anteontem e 2 ontem, sempre para o mesmo
        destino. Cada resposta mede o histórico ANTERIOR a ela, nos dois
        eixos — o destino recebeu os mesmos Pix que o pagador mandou.
        """
        payer, recipient = new_payer(), new_recipient()

        history = seed_pix(payer, recipient, spread_over_days([5, 2]))

        # 1º Pix: nada antes. 2º ao 6º: de 1 a 5 antes, low. 7º: 6 antes,
        # em 2 dias — o primeiro medium.
        expected = ["unavailable", "low", "low", "low", "low", "low", "medium"]
        assert [pix.sufficiency["payer"] for pix in history] == expected
        assert [pix.sufficiency["recipient"] for pix in history] == expected

    def test_suficiencia_fica_low_com_6_pix_num_dia_so(self):
        """6 Pix anteriores num dia só ainda é low: medium pede 2 dias ou mais."""
        payer, recipient = new_payer(), new_recipient()

        history = seed_pix(payer, recipient, spread_over_days([7]))

        # O 7º Pix do dia tem 6 antes dele, todos no mesmo dia.
        assert history[-1].sufficiency == {"payer": "low", "recipient": "low"}

    def test_suficiencia_high_pede_30_pix_em_7_dias(self):
        """High pede 30 Pix anteriores em 7 dias ou mais. Com 29, medium.

        31 Pix em 7 dias: 4 por dia nos seis primeiros, 7 no último.
        """
        payer, recipient = new_payer(), new_recipient()

        history = seed_pix(payer, recipient, spread_over_days([4, 4, 4, 4, 4, 4, 7], first_hour=8))

        # O 30º Pix já tem 7 dias de histórico, mas só 29 Pix atrás dele.
        assert history[29].sufficiency == {"payer": "medium", "recipient": "medium"}
        # O 31º tem 30, em 7 dias.
        assert history[30].sufficiency == {"payer": "high", "recipient": "high"}
