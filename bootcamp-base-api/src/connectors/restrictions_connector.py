"""As restrições internas: alguém desta transação está bloqueado?

É a fonte do D-RESTRICT-SIM, a primeira linha da tabela de decisão, e a
única fonte obrigatória: restrição ativa rejeita por cima de qualquer
nota, e sem a lista não há como saber se existe uma. Por isso a falha
aqui não vira unavailable — vira RestrictionSourceUnavailable, 503, e o
Pix sai sem veredito e sem ficar gravado (RFC §7, prioridade 2: nem
presumir lista vazia, nem deixar a nota decidir). O DICT e o cadastro de
aparelhos podem faltar; esta não.

O combinado: GET /restrictions?payer=&recipient_holder=&recipient_account=
responde 200 com {"restrictions": [...]}, a lista (quase sempre vazia) do
que a fonte tem contra os três — o pagador e o titular pelo documento, a
conta exata por ispb:agência:conta. Este connector devolve a lista como
veio, já convertida; quem decide quais valem agora, e pra quem, é o
controller, com a vigência e o alcance que moram na própria Restriction
(antifraud/contract.py).
"""

from urllib.parse import urlencode

import requests

from antifraud.contract import Restriction, RestrictionScope
from connectors.rest_connector import RestConnector, parse_instant
from constants import RESTRICTIONS_URL, SOURCES_INTERNAL_TOKEN, SOURCES_TIMEOUT
from errors import RestrictionSourceUnavailable
from utils.document_number import mask_document

_TEXT_FIELDS = ("restriction_id", "scope", "identity_key", "reason", "evidence_ref", "provenance")


class RestrictionsConnector(RestConnector):
    """Fala com a fonte de restrições — aqui, com o mock dela."""

    def __init__(self) -> None:
        super().__init__(
            class_name=__name__,
            base_url=RESTRICTIONS_URL,
            timeout=SOURCES_TIMEOUT,
            internal_token=SOURCES_INTERNAL_TOKEN,
        )

    def list_restrictions(
        self, *, payer_document: str, recipient_document: str, recipient_account_key: str,
    ) -> tuple[Restriction, ...]:
        """As restrições que a fonte tem contra o pagador, o titular do destino ou a conta exata.

        Os documentos chegam só com dígitos; a conta exata, como
        ispb:agência:conta. Qualquer falha levanta
        RestrictionSourceUnavailable.
        """
        query = {"payer": payer_document, "recipient_holder": recipient_document, "recipient_account": recipient_account_key}
        masked = {**query, "payer": mask_document(payer_document), "recipient_holder": mask_document(recipient_document)}

        try:
            response = self.send(
                endpoint=f"/restrictions?{urlencode(query)}",
                method="GET",
                log_as=f"/restrictions?{urlencode(masked)}",
            )
        except requests.RequestException as error:
            raise self._unavailable(f"request failed ({type(error).__name__})") from None

        if response.status != 200:
            raise self._unavailable(f"status {response.status}")

        body = response.json if isinstance(response.json, dict) else {}
        items = body.get("restrictions")
        if not isinstance(items, list):
            raise self._unavailable("body without a restrictions list")

        # Uma restrição torta invalida a resposta inteira: a lista é o que
        # diz quem está bloqueado, e metade de uma lista não diz nada.
        try:
            return tuple(_restriction(item) for item in items)
        except (KeyError, TypeError, ValueError):
            raise self._unavailable("body with a restriction out of the agreed format") from None

    def _unavailable(self, reason: str) -> RestrictionSourceUnavailable:
        self.logger.warning(f"RESTRICTIONS UNAVAILABLE - {reason}")
        return RestrictionSourceUnavailable(reason)


def _restriction(item) -> Restriction:
    if not isinstance(item, dict):
        raise TypeError("restriction is not an object")
    texts = {name: item[name] for name in _TEXT_FIELDS}
    if any(not isinstance(value, str) or not value for value in texts.values()):
        raise TypeError("restriction with a text field that is not text")
    return Restriction(
        restriction_id=texts["restriction_id"],
        scope=RestrictionScope(texts["scope"]),
        identity_key=texts["identity_key"],
        reason=texts["reason"],
        evidence_ref=texts["evidence_ref"],
        provenance=texts["provenance"],
        effective_at=parse_instant(item["effective_at"]),
        expires_at=parse_instant(item.get("expires_at"), optional=True),
        revoked_at=parse_instant(item.get("revoked_at"), optional=True),
    )
