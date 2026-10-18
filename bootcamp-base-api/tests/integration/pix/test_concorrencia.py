"""Pix chegando ao mesmo tempo.

As threads mandam os POSTs juntas, soltas por uma barreira: todas esperam
na linha de largada e saem no mesmo instante. O que se prova:
  • dois Pix diferentes do mesmo pagador passam um de cada vez — a trava
    por pagador — e o segundo vê o primeiro no histórico;
  • o mesmo id chegando várias vezes ao mesmo tempo vira UMA análise, e
    nunca 500: 200 pro mesmo Pix, 409 pra outro conteúdo;
  • Pix cruzados de gente nova (A paga B enquanto B paga A) não travam
    um no outro.
As corridas que dependem de sorte rodam algumas vezes dentro do teste:
uma rodada sem a trava ainda pode sair certa por acaso; três seguidas,
quase nunca.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

from tests.utils.clock import T0, minutes_before
from tests.utils.identities import new_payer, new_recipient
from tests.utils.pix import PixResponse, get_pix, pix_payload, post_pix, seed, seed_pix

ROUNDS = 3


def post_together(payloads: list[dict]) -> list[PixResponse]:
    """Manda os payloads ao mesmo tempo, um por thread, e devolve as respostas na ordem."""
    start = Barrier(len(payloads))

    def send(payload: dict) -> PixResponse:
        start.wait(timeout=10)
        return post_pix(payload)

    with ThreadPoolExecutor(max_workers=len(payloads)) as pool:
        return list(pool.map(send, payloads))


class TestConcorrencia:
    def test_concorrencia_mesmo_pagador_conta_uma_vez(self):
        """Cenário 15 — dois Pix do mesmo pagador ao mesmo tempo, na fronteira do P04.

        Preparo: 3 Pix nos 3 minutos antes de T0. Depois, dois Pix
        diferentes, os dois datados de T0, saem juntos. Com a trava por
        pagador, um espera o outro: o primeiro vê 3 anteriores (3 + 1 = 4,
        P04 calado) e o segundo vê 4 (4 + 1 = 5, P04 dispara). Exatamente
        um dispara, nas três rodadas.

        Repare no relógio de recebimento ordenado: o segundo que adquire a
        trava vê o primeiro por received_at. Sem a trava, os
        dois leriam o histórico antes de qualquer um gravar, veriam 3, e
        nenhum dispararia — a rajada passaria pela fresta.
        """
        for _ in range(ROUNDS):
            payer, recipient = new_payer(), new_recipient()
            seed_pix(payer, recipient, minutes_before(T0, 3))

            responses = post_together([pix_payload(payer, recipient, T0), pix_payload(payer, recipient, T0)])

            assert [pix.http_status for pix in responses] == [201, 201]
            assert sorted(pix.signal_status("P04") for pix in responses) == ["not_triggered", "triggered"]
            counted = next(pix for pix in responses if pix.signal_status("P04") == "triggered")
            assert counted.signal("P04")["evidence"]["attempts_5m"] == 5

    def test_mesmo_pix_ao_mesmo_tempo_vira_uma_analise_so(self):
        """O mesmo Pix 4 vezes ao mesmo tempo: um 201, três 200, o mesmo corpo.

        Do mesmo pagador, a trava põe os quatro em fila, e quem passa
        depois do primeiro acha o id já gravado: lê a análise de quem
        ganhou e devolve essa, como num reenvio.
        """
        payload = pix_payload(new_payer(), new_recipient(), T0)

        responses = post_together([payload] * 4)

        assert sorted(pix.http_status for pix in responses) == [200, 200, 200, 201]
        bodies = [pix.body for pix in responses]
        assert all(body == bodies[0] for body in bodies)
        assert get_pix(payload["id"]).body == bodies[0]

    def test_mesmo_id_por_dois_pagadores_ao_mesmo_tempo_volta_201_e_409(self):
        """O mesmo id vindo de dois pagadores ao mesmo tempo: um grava, o outro recebe 409.

        A trava é por pagador, e dois pagadores não esperam um pelo
        outro: os dois chegam juntos ao INSERT, e quem resolve é o UNIQUE
        do id. Quem perde não vê 500 — lê a análise de quem ganhou, que
        tem outro conteúdo, e responde 409. O GET devolve a do 201.

        Customers e contas são criados antes, pelas sementes: sem isso, o
        primeiro Pix de cada um criaria as linhas, e a criação já poria os
        dois em fila antes do INSERT — a corrida nem aconteceria.
        """
        for _ in range(ROUNDS):
            first, second, recipient = new_payer(), new_payer(), new_recipient()
            seed([
                pix_payload(first, recipient, T0 - timedelta(days=2)),
                pix_payload(second, recipient, T0 - timedelta(days=1)),
            ])
            shared_id = pix_payload(first, recipient)["id"]

            responses = post_together([
                pix_payload(first, recipient, T0, pix_id=shared_id),
                pix_payload(second, recipient, T0, pix_id=shared_id),
            ])

            assert sorted(pix.http_status for pix in responses) == [201, 409]
            winner = next(pix for pix in responses if pix.http_status == 201)
            loser = next(pix for pix in responses if pix.http_status == 409)
            assert loser.body["code"] == "QIT001002"
            assert get_pix(shared_id).content == winner.content

    def test_pix_cruzados_de_gente_nova_ao_mesmo_tempo_voltam_201(self):
        """50 pares de gente nova, A pagando B enquanto B paga A, 16 de cada vez: os 100 voltam 201.

        No primeiro Pix de cada um, a API cria o customer. Criando na ordem
        do Pix — pagador primeiro —, A→B e B→A esperariam um pelo outro,
        cada um segurando a linha que o outro quer: deadlock, e um 500. A
        API cria na ordem do documento, e o segundo só espera o primeiro.
        """
        payloads = []
        for _ in range(50):
            first, second = new_payer(), new_payer()
            payloads.append(pix_payload(first, new_recipient(document=second.document), T0))
            payloads.append(pix_payload(second, new_recipient(document=first.document), T0))

        with ThreadPoolExecutor(max_workers=16) as pool:
            statuses = list(pool.map(lambda payload: post_pix(payload).http_status, payloads))

        assert statuses == [201] * len(payloads)
