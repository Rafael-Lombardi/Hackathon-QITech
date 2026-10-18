"""O recibo de cada id: a prova de que o Pix chegou, antes e independente da avaliação.

Um POST reivindica o recibo do id antes de consultar fonte ou travar
pagador (RFC §8). Enquanto a avaliação corre, o mesmo Pix de novo — ou o
GET — recebe 409 "em processamento" com Retry-After, e nunca um segundo
avaliador; gravada a análise, o reenvio é 200 com os mesmos bytes; se a
avaliação falhou, o GET diz isso (503) e o reenvio avalia de novo
(test_restricoes.py). Pra segurar uma avaliação no ar por tempo
suficiente, o DICT do recebedor responde depois do prazo da API: os 2 s
em que a API espera por ele são a janela destes testes.
"""

import time
from concurrent.futures import ThreadPoolExecutor

from tests.utils.clock import T0
from tests.utils.identities import new_payer, new_recipient
from tests.utils.pix import get_pix, pix_payload, post_pix

ERROR_KEYS = {"title", "description", "translation", "code"}


class TestRecibo:
    def test_mesmo_pix_durante_a_avaliacao_recebe_409_em_processamento(self, sources_mock):
        """C1 — o mesmo Pix de novo enquanto o primeiro espera o DICT: 201 e 409 QIT001006, e depois 200.

        O DICT do recebedor demora 3 s (a API desiste em 2). O primeiro
        POST reivindica o recibo e fica esperando; meio segundo depois, o
        mesmo Pix chega de novo e encontra o recibo em processing com o
        lease vigente: 409 com Retry-After e sem campos de sucesso, na
        hora — ele não espera o primeiro. O GET nesse meio tempo responde
        o mesmo 409. Terminada a avaliação (DICT01 unavailable, D-OK), o
        reenvio é 200 com os mesmos bytes, e o GET também.
        """
        payer, recipient = new_payer(), new_recipient()
        sources_mock.set_delay(recipient.document, 3)
        payload = pix_payload(payer, recipient, T0)

        with ThreadPoolExecutor(max_workers=1) as pool:
            first = pool.submit(post_pix, payload)
            time.sleep(0.5)
            during = post_pix(payload)
            found_during = get_pix(payload["id"])
            first = first.result()
        again = post_pix(payload)

        assert first.http_status == 201
        assert first.signal_status("DICT01") == "unavailable"
        assert first.decision_rule == "D-OK"
        assert during.http_status == 409
        assert during.body["code"] == "QIT001006"
        assert set(during.body) == ERROR_KEYS
        assert during.headers["Retry-After"] == "1"
        assert found_during.http_status == 409
        assert found_during.body["code"] == "QIT001006"
        assert again.http_status == 200
        assert again.content == first.content
        assert get_pix(payload["id"]).content == first.content

    def test_outro_conteudo_durante_a_avaliacao_e_409_de_conflito(self, sources_mock):
        """O mesmo id com outro conteúdo enquanto o primeiro ainda é avaliado: 409 de conflito, e não "em processamento".

        O conteúdo é conferido antes do estado do recibo: id reaproveitado
        é conflito em qualquer estado (RFC §8), e quem reaproveitou não
        ganha um Retry-After que nunca vai dar em nada.
        """
        payer, recipient = new_payer(), new_recipient()
        sources_mock.set_delay(recipient.document, 3)
        payload = pix_payload(payer, recipient, T0)
        changed = pix_payload(payer, recipient, T0, amount=payload["amount"] + 100, pix_id=payload["id"])

        with ThreadPoolExecutor(max_workers=1) as pool:
            first = pool.submit(post_pix, payload)
            time.sleep(0.5)
            conflict = post_pix(changed)
            first = first.result()

        assert first.http_status == 201
        assert conflict.http_status == 409
        assert conflict.body["code"] == "QIT001002"
        assert "Retry-After" not in conflict.headers
