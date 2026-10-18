"""O que entra no histórico, e o que não entra.

As regras da camada 2 comparam o Pix com o passado, e quase todo erro de
antifraude mora na definição de passado. As que o motor usa (o bloco
"Janelas de histórico" de src/antifraud/policy.py):
  • anterior é o que foi recebido até o instante da avaliação, pelo
    relógio do servidor (na stack de teste, o que o post_pix manda no
    header) — nada recebido depois conta, seja qual for a data no payload;
  • rajada e montante (P04, P05, R02, R03...) contam TODA tentativa, até
    as barradas: o invasor insistente não pode sumir depois do 1º 2FA;
  • IP conhecido, par recorrente e mediana contam só Pix aprovados: um
    2FA que ninguém confirmou não ensina nada ao motor;
  • o aparelho não é histórico: cadastrado é o que o BANCO cadastrou
    (test_cadastro_aparelhos.py), e tentativa nenhuma cadastra — o
    histórico só diz se o par já apareceu (previously_observed);
  • o pagador é o documento, e o destino tem duas identidades: a conta
    (R01) e o titular (R02, R03).
Cada teste abaixo prova uma dessas linhas pela porta da frente.
"""

from dataclasses import replace
from datetime import timedelta
from ipaddress import ip_address

from tests.utils.clock import NINE_OVER_SEVEN_DAYS, T0, days_ago, days_before, minutes_before, spread_over_days
from tests.utils.identities import new_device, new_ip, new_ipv6, new_payer, new_recipient
from tests.utils.pix import pix_payload, post_pix, seed, seed_pix


class TestHistorico:
    def test_2fa_nao_confirmado_nao_cadastra_o_aparelho(self):
        """Aparelho cadastrado é o que o BANCO cadastrou: tentativa não cadastra, nem a que ficou em 2FA.

        Preparo: 3 Pix nos 3 dias anteriores. Às 14h, um Pix de R$ 50 de
        aparelho sem cadastro e IP novo: P03 +18, P09 +6, P10 +12 —
        D-ATO-2, 2FA. Às 14h10, o mesmo aparelho e o mesmo IP tentam de
        novo. O aparelho continua sem cadastro (o 2FA nunca foi confirmado,
        e nem confirmado cadastraria: isso é do banco) e o IP continua sem
        Pix aprovado: os mesmos três sinais, e só eles (R$ 50 fica abaixo
        do L02), D-ATO-2 de novo. O que muda é previously_observed: o par
        já tinha aparecido, e a resposta diz isso sem confundir com
        cadastro (regressão A da RFC).

        Se qualquer tentativa cadastrasse o aparelho, a segunda do invasor
        passaria limpa.
        """
        payer, recipient = new_payer(), new_recipient()
        seed_pix(payer, recipient, days_before(T0, 3))
        intruder = {"device_id": new_device(), "ip": new_ip()}

        first = post_pix(pix_payload(payer, recipient, T0, **intruder))
        second = post_pix(pix_payload(payer, recipient, T0 + timedelta(minutes=10), **intruder))

        assert first.status == "two_factor_required"
        assert first.decision_rule == "D-ATO-2"
        assert first.metadata["device"] == {
            "registration_status": "unregistered", "registration_source": "demo_seed", "previously_observed": False,
        }
        assert second.http_status == 201
        assert second.status == "two_factor_required"
        assert second.decision_rule == "D-ATO-2"
        assert second.triggered() == {"P03", "P09", "P10"}
        assert second.signal("P03")["evidence"]["previously_observed"] is True
        assert second.metadata["device"] == {
            "registration_status": "unregistered", "registration_source": "demo_seed", "previously_observed": True,
        }

    def test_cliente_novo_com_3_pix_em_2fa_nao_cai_no_d_ato_2(self, sources_mock):
        """O IP só é julgado depois de 3 Pix APROVADOS — tentativa não basta; o aparelho responde pelo cadastro.

        O resultado do 2FA não volta pra esta API. Se a pré-condição do IP
        contasse tentativas, um cliente novo com os 3 primeiros Pix em 2FA
        teria, no 4º, um IP "sem Pix aprovado" do próprio celular — e, se
        o aparelho também fosse julgado por histórico, P03 e P09 juntos,
        D-ATO-2, preso no 2FA pra sempre.

        Preparo: um cliente novo manda 3 Pix, nos 3 dias anteriores, pra
        uma conta aberta há 2 dias que o DICT avalia com confiança 12. Os 3
        terminam em 2FA pelo D-RCP (R01 12 + DICT01 32 = 44, e REL01 +8 do
        2º em diante). Pix: o 4º, do mesmo aparelho e IP.

            P03, L02     calados: o aparelho é o cadastrado, e o cadastro
                         não depende de histórico
            P09, P10     unavailable: 3 tentativas, 0 aprovadas
            destino 12 + 32 + 8 = 52

        2FA de novo, mas pelo D-RCP — o destino continua suspeito —, e não
        pelo D-ATO-2.
        """
        payer = new_payer()
        fresh = new_recipient(opening_date=T0 - timedelta(days=2))
        sources_mock.set_security_statistics(fresh.document, 12)
        seed_pix(payer, fresh, days_before(T0, 3), expect="two_factor_required")

        pix = post_pix(pix_payload(payer, fresh, T0))

        assert pix.http_status == 201
        assert {pix.signal_status(code) for code in ("P03", "L02")} == {"not_triggered"}
        assert {pix.signal_status(code) for code in ("P09", "P10")} == {"unavailable"}
        assert pix.scores["recipient"] == 52
        assert pix.status == "two_factor_required"
        assert pix.decision_rule == "D-RCP"

    def test_p06_so_conta_pix_aprovados(self):
        """A mediana e o lastro dela só contam Pix aprovados.

        Preparo: 9 Pix de R$ 200 aprovados em 7 dias, e um 10º, ontem à
        noite, de aparelho sem cadastro e IP novo, que terminou em 2FA. São 10
        tentativas em 7 dias, mas 9 aprovadas. Pix: R$ 1.500 do aparelho de
        sempre — 7,5x a mediana —, e o P06 diz unavailable: falta lastro.
        Contando a tentativa barrada, ele dispararia.
        """
        payer, recipient = new_payer(), new_recipient()
        seed_pix(payer, recipient, spread_over_days(NINE_OVER_SEVEN_DAYS), amount=20000)
        seed_pix(
            payer, recipient, [T0 - timedelta(hours=18)],
            expect="two_factor_required", amount=20000, device_id=new_device(), ip=new_ip(),
        )

        pix = post_pix(pix_payload(payer, recipient, T0, amount=150000))

        assert pix.http_status == 201
        assert pix.signal_status("P06") == "unavailable"
        assert pix.status == "approved"

    def test_rel02_suspenso_por_2fa_recente_no_par(self):
        """O desconto da relação recorrente some se o par teve 2FA na última semana.

        Preparo: 3 Pix aprovados para o mesmo destino, há 10, 5 e 2 dias (o
        REL02 já valeria). Ontem, um Pix para ele de aparelho sem cadastro
        e IP novo: D-ATO-2, 2FA. Pix: o de sempre — e o REL02 fica calado. A relação
        antiga é exatamente o que um golpista quer parecer.
        """
        payer, recipient = new_payer(), new_recipient()
        seed_pix(payer, recipient, days_ago(T0, 10, 5, 2))
        seed_pix(
            payer, recipient, [T0 - timedelta(days=1)],
            expect="two_factor_required", device_id=new_device(), ip=new_ip(),
        )

        pix = post_pix(pix_payload(payer, recipient, T0))

        assert pix.http_status == 201
        assert pix.signal_status("REL02") == "not_triggered"
        assert pix.signal_status("REL01") == "not_triggered"
        assert pix.status == "approved"

    def test_rajada_conta_toda_tentativa_mesmo_as_barradas(self):
        """O P04 conta tentativas, e não aprovações.

        Preparo: 3 Pix nos 3 dias anteriores. Nos 4 minutos antes de T0, 4
        tentativas, cada uma de um aparelho sem cadastro e IP novo: as 4
        terminam em 2FA. Pix: o 5º, do aparelho cadastrado — P04 dispara
        com 5. Contar
        só aprovados deixaria o atacante insistente invisível.
        """
        payer, recipient = new_payer(), new_recipient()
        seed_pix(payer, recipient, days_before(T0, 3))
        seed(
            [pix_payload(payer, recipient, moment, device_id=new_device(), ip=new_ip()) for moment in minutes_before(T0, 4)],
            expect="two_factor_required",
        )

        pix = post_pix(pix_payload(payer, recipient, T0))

        assert pix.http_status == 201
        assert pix.signal_status("P04") == "triggered"
        assert pix.signal("P04")["evidence"]["attempts_5m"] == 5
        assert pix.status == "approved"

    def test_conta_conjunta_cada_titular_tem_o_proprio_historico(self):
        """Duas pessoas na mesma conta de origem são dois pagadores, cada um com o seu passado.

        Preparo: A faz 3 Pix pela conta dele, nos 3 dias anteriores. B, com
        outro CPF, usa a MESMA agência e conta 5 vezes nos 5 minutos antes
        de T0 — conta conjunta. Pix: o de A, pela conta, às 14h.

            P04 calado   as 5 tentativas foram de B, e não contam pra A
            suficiência  pagador low: os 3 Pix de A, e só eles

        Se a conta carregasse o histórico de quem a usou primeiro, A
        herdaria a rajada de B, e o P04 dispararia.
        """
        holder, recipient = new_payer(), new_recipient()
        seed_pix(holder, recipient, days_before(T0, 3))
        co_holder = replace(new_payer(), account=holder.account)
        seed_pix(co_holder, new_recipient(), minutes_before(T0, 5))

        pix = post_pix(pix_payload(holder, recipient, T0))

        assert pix.http_status == 201
        assert pix.signal_status("P04") == "not_triggered"
        assert pix.sufficiency["payer"] == "low"

    def test_r01_olha_a_conta_e_r03_olha_o_titular(self):
        """As duas identidades do destino (RFC v0.5): a conta e o titular.

        O titular tem uma conta antiga e uma aberta há 2 dias. Três
        pagadores novos pagam ele nos últimos 50 minutos, nas duas contas.

            4º pagador, conta ANTIGA   R03 +20 (4 pagadores no titular),
                                       R01 calado: destino 20, aprovado
            5º pagador, conta NOVA     R03 +20, R01 +12, R04 +15
                                       12 + 20 + 15 = 47, teto do
                                       recipient_network: 40 → D-RCP, 2FA

        O mesmo titular, a mesma hora: o que muda a decisão é a conta,
        porque é a conta nova concentrando pagadores que bate no teto.
        """
        old = new_recipient()
        new = old.with_new_account(opening_date=T0 - timedelta(days=2))
        seed([
            pix_payload(new_payer(), account, T0 - timedelta(minutes=minutes))
            for account, minutes in ((old, 50), (new, 40), (old, 30))
        ])

        to_old = post_pix(pix_payload(new_payer(), old, T0))
        to_new = post_pix(pix_payload(new_payer(), new, T0 + timedelta(minutes=1)))

        assert to_old.http_status == 201
        assert to_old.triggered() == {"R03"}
        assert to_old.scores["recipient"] == 20
        assert to_old.status == "approved"
        assert to_new.http_status == 201
        assert to_new.triggered() == {"R01", "R03", "R04"}
        assert to_new.scores["recipient"] == 40
        assert to_new.status == "two_factor_required"
        assert to_new.decision_rule == "D-RCP"

    def test_pix_recebido_depois_nao_entra_no_historico(self):
        """Histórico se conta pelo relógio do servidor, e nada recebido depois de t é anterior.

        Na stack de teste o relógio do servidor é o header que o post_pix
        manda — a própria transaction_date. Chegam 4 Pix recebidos de T0+1
        a T0+4 minutos; depois, um recebido em T0. Para ele, nenhum dos 4
        é anterior: o pagador está no primeiro Pix da vida — P04
        unavailable, suficiência unavailable.
        """
        payer, recipient = new_payer(), new_recipient()
        seed_pix(payer, recipient, [T0 + timedelta(minutes=minutes) for minutes in (1, 2, 3, 4)])

        pix = post_pix(pix_payload(payer, recipient, T0))

        assert pix.http_status == 201
        assert pix.signal_status("P04") == "unavailable"
        assert pix.sufficiency["payer"] == "unavailable"

    def test_ipv6_escrito_de_outro_jeito_e_o_mesmo_ip(self):
        """2001:DB8:0:0:0:0:0:1 e 2001:db8::1 são o mesmo endereço, e o histórico sabe disso.

        Preparo: 3 Pix com um IPv6 escrito em minúsculas e abreviado. Pix:
        o mesmo IP por extenso, com todos os zeros, e em maiúsculas. P09
        calado: o IP já tem Pix aprovado.
        """
        payer, recipient = new_payer(ip=new_ipv6()), new_recipient()
        seed_pix(payer, recipient, days_before(T0, 3))
        same_ip = ip_address(payer.ip).exploded.upper()
        assert same_ip != payer.ip

        pix = post_pix(pix_payload(payer, recipient, T0, ip=same_ip))

        assert pix.http_status == 201
        assert pix.signal_status("P09") == "not_triggered"
