"""As restrições internas: a primeira linha da tabela, e a única fonte obrigatória.

Restrição ativa contra o pagador, o titular do destino ou a conta exata
de destino rejeita por cima de qualquer nota, até com o cadastro de
aparelhos fora do ar (D-RESTRICT-SIM). Restrição futura, vencida ou
revogada não vale, e a da conta exata não é do titular. E a fonte fora do
ar é 503 sem veredito: o Pix não fica gravado, e quem chamou tenta de
novo (RFC §7, casos R1 a R3). Cada teste escreve a restrição dele no mock
pela fixture sources_mock.
"""

from datetime import timedelta

import pytest

from tests.utils.clock import T0, days_before
from tests.utils.identities import Recipient, new_payer, new_recipient
from tests.utils.pix import get_pix, pix_payload, post_pix, seed_pix

ERROR_KEYS = {"title", "description", "translation", "code"}


def _account_key(recipient: Recipient) -> str:
    """A conta exata como a fonte de restrições a identifica: ispb:agência:conta."""
    account = recipient.account
    return f"{account.participant}:{account.branch}:{account.account_number}"


def _as_returned(restriction: dict) -> dict:
    """A restrição como a API a devolve: tudo menos a identity_key, que é documento."""
    return {name: value for name, value in restriction.items() if name != "identity_key"}


class TestRestricoes:
    @pytest.mark.parametrize(
        "scope",
        ["payer", "recipient_holder", "recipient_account"],
        ids=["pagador", "titular-do-destino", "conta-exata-de-destino"],
    )
    def test_restricao_ativa_rejeita(self, sources_mock, scope):
        """Restrição ativa contra alguém do Pix: rejeitado pelo D-RESTRICT-SIM, nota nenhuma importa.

        Um pagador com 3 Pix nos 3 dias anteriores manda R$ 50 pro destino
        de sempre — o Pix mais inocente da suíte, pagador 0 e destino 0 —
        e a fonte tem uma restrição ativa contra o pagador, o titular do
        destino ou a conta exata. Rejeitado, com a restrição na resposta
        (sem o documento) e o motivo dizendo qual. Os 24 sinais continuam
        na resposta: a restrição decide, mas não cala o motor. O GET
        devolve o mesmo.
        """
        payer, recipient = new_payer(), new_recipient()
        seed_pix(payer, recipient, days_before(T0, 3))
        identity = {
            "payer": payer.document,
            "recipient_holder": recipient.document,
            "recipient_account": _account_key(recipient),
        }[scope]
        restriction = sources_mock.restrict(scope, identity, reason="conta bloqueada por ordem judicial")
        payload = pix_payload(payer, recipient, T0)

        pix = post_pix(payload)

        assert pix.http_status == 201
        assert pix.status == "rejected"
        assert pix.decision_rule == "D-RESTRICT-SIM"
        assert pix.reasons == [f"Restrição ativa: conta bloqueada por ordem judicial ({scope})"]
        assert pix.metadata["restrictions"] == [_as_returned(restriction)]
        assert pix.scores == {"payer": 0, "recipient": 0}
        assert len(pix.metadata["signals"]) == 24
        assert get_pix(payload["id"]).content == pix.content

    def test_restricao_prevalece_sobre_cadastro_fora_do_ar(self, sources_mock):
        """R1 — restrição ativa com o cadastro de aparelhos fora do ar: D-RESTRICT-SIM, e não o fallback.

        O mesmo Pix inocente, com a fonte do cadastro respondendo 503 e
        uma restrição ativa contra o pagador. A restrição vem antes de
        tudo na tabela; o D-DATA-UNAVAILABLE só existiria no lugar do D-OK.
        """
        payer, recipient = new_payer(), new_recipient()
        sources_mock.registry_down(payer.document)
        sources_mock.restrict("payer", payer.document)

        pix = post_pix(pix_payload(payer, recipient, T0))

        assert pix.http_status == 201
        assert pix.status == "rejected"
        assert pix.decision_rule == "D-RESTRICT-SIM"
        assert pix.signal_status("P03") == "unavailable"

    @pytest.mark.parametrize(
        "dates",
        [
            {"effective_at": T0 + timedelta(hours=1)},
            {"expires_at": T0 - timedelta(hours=1)},
            {"expires_at": T0},
            {"revoked_at": T0 - timedelta(hours=1)},
            {"revoked_at": T0},
        ],
        ids=["futura", "vencida-ha-1h", "vence-neste-instante", "revogada-ha-1h", "revogada-neste-instante"],
    )
    def test_restricao_fora_da_vigencia_nao_rejeita(self, sources_mock, dates):
        """R2 — restrição que ainda não começou, já venceu ou foi revogada não vale: D-OK, lista vazia.

        A vigência é a da RFC §7: effective_at <= t, t < expires_at e
        t < revoked_at, com t o instante do Pix. Vencer ou ser revogada
        exatamente em t já tira a restrição do jogo.
        """
        payer, recipient = new_payer(), new_recipient()
        sources_mock.restrict("payer", payer.document, **dates)

        pix = post_pix(pix_payload(payer, recipient, T0))

        assert pix.http_status == 201
        assert pix.status == "approved"
        assert pix.decision_rule == "D-OK"
        assert pix.metadata["restrictions"] == []

    def test_restricao_que_comeca_neste_instante_ja_vale(self, sources_mock):
        """O outro lado da fronteira do começo: effective_at igual a t já rejeita."""
        payer, recipient = new_payer(), new_recipient()
        sources_mock.restrict("payer", payer.document, effective_at=T0)

        pix = post_pix(pix_payload(payer, recipient, T0))

        assert pix.http_status == 201
        assert pix.status == "rejected"
        assert pix.decision_rule == "D-RESTRICT-SIM"

    def test_restricao_da_conta_exata_nao_alcanca_o_titular(self, sources_mock):
        """Conta exata e titular são identidades diferentes: a restrição numa conta não bloqueia outra do mesmo titular.

        A fonte restringe a conta A do titular. Pix pra conta B dele,
        noutro banco: D-OK, sem restrição na resposta. Pix pra A:
        rejeitado.
        """
        payer, holder = new_payer(), new_recipient()
        other_account = holder.with_new_account()
        sources_mock.restrict("recipient_account", _account_key(holder))

        to_other = post_pix(pix_payload(payer, other_account, T0))
        to_restricted = post_pix(pix_payload(payer, holder, T0 + timedelta(minutes=1)))

        assert to_other.http_status == 201
        assert to_other.status == "approved"
        assert to_other.metadata["restrictions"] == []
        assert to_restricted.http_status == 201
        assert to_restricted.status == "rejected"
        assert to_restricted.decision_rule == "D-RESTRICT-SIM"

    def test_fonte_de_restricoes_fora_do_ar_e_503_sem_veredito(self, sources_mock):
        """R3 — a fonte obrigatória falhou: 503 QIT001005, e o recibo fica failed até o reenvio.

        Sem a lista não há como saber se existe restrição, e presumir
        lista vazia deixaria passar justamente quem está bloqueado. A API
        não decide: 503, erro no formato do Base Service, e nenhuma
        análise gravada — o GET do id responde 503 QIT001007 (chegou, e a
        avaliação falhou), e não 404. Quando a fonte volta, o mesmo Pix
        reenviado reivindica o recibo e é avaliado: 201 (RFC §8, C2), e o
        GET passa a devolver a análise.
        """
        payer, recipient = new_payer(), new_recipient()
        sources_mock.restrictions_down(payer.document)
        payload = pix_payload(payer, recipient, T0)

        refused = post_pix(payload)
        failed = get_pix(payload["id"])
        sources_mock.clear()
        retried = post_pix(payload)

        assert refused.http_status == 503
        assert refused.body["code"] == "QIT001005"
        assert set(refused.body) == ERROR_KEYS
        assert failed.http_status == 503
        assert failed.body["code"] == "QIT001007"
        assert retried.http_status == 201
        assert retried.status == "approved"
        assert get_pix(payload["id"]).content == retried.content
