"""O cadastro de aparelhos do banco: este aparelho é deste cliente?

É a fonte do P03 e, por ele, do L02 e do P10. O banco cadastra o aparelho
do cliente na ativação do app, e um Pix saindo de um aparelho que o banco
não conhece é o primeiro sinal de conta tomada. Aqui quem responde é o
mock do docker-compose.yml (mockserver/README.md), e a premissa que vale
pra ele vale pra fixture de qualquer demonstração: a fixture É o cadastro
completo dos pagadores que ela cobre. Pagador coberto sem o aparelho é
aparelho sem cadastro, de verdade; pagador fora da fixture não é nada — a
RFC manda não ler ausência em dado parcial como não-cadastro —, e por
isso o 404 vira uncovered, e nunca unregistered.

O combinado: GET /device-registration/{documento}/{aparelho} responde 200
com registration_status "registered" (mais registered_at, revoked_at nula
ou não, e provenance) ou "unregistered"; 404 quando o pagador não está na
fixture. Prazo estourado, 5xx ou corpo fora do combinado é a fonte
indisponível: unavailable, e mais nada — o que isso significa pro Pix (o
P03 unavailable, e o D-DATA-UNAVAILABLE no lugar do D-OK) quem decide é
o motor. Repare que este connector não levanta erro de fonte, ao
contrário do DictConnector: as quatro respostas cabem num status só, e o
controller não teria o que capturar.
"""

from dataclasses import dataclass
from datetime import datetime
from urllib.parse import quote

import requests

from antifraud.contract import RegistrationStatus
from connectors.rest_connector import RestConnector, parse_instant
from constants import DEVICE_REGISTRY_URL, SOURCES_INTERNAL_TOKEN, SOURCES_TIMEOUT
from utils.document_number import mask_document


@dataclass(frozen=True)
class DeviceRegistryRecord:
    """O que o cadastro respondeu sobre um par pagador–aparelho.

    status é a resposta como veio. As datas só existem no registered, e
    são o que dá vigência ao cadastro: o aparelho cadastrado ontem não
    estava cadastrado anteontem, e o revogado às 10h não está às 11h. Por
    isso quem lê o status passa por status_at, com o instante da
    avaliação — a resposta é pedida antes da trava do pagador, e o
    instante só é fixado depois dela.
    """

    status: RegistrationStatus
    registered_at: datetime | None = None
    revoked_at: datetime | None = None
    provenance: str | None = None

    def status_at(self, moment: datetime) -> RegistrationStatus:
        """O status em `moment`: registered só dentro da vigência (RFC §3)."""
        if self.status is not RegistrationStatus.REGISTERED:
            return self.status
        if self.registered_at <= moment and (self.revoked_at is None or self.revoked_at > moment):
            return RegistrationStatus.REGISTERED
        return RegistrationStatus.UNREGISTERED


class DeviceRegistryConnector(RestConnector):
    """Fala com o cadastro de aparelhos do banco — aqui, com o mock dele."""

    def __init__(self) -> None:
        super().__init__(
            class_name=__name__,
            base_url=DEVICE_REGISTRY_URL,
            timeout=SOURCES_TIMEOUT,
            internal_token=SOURCES_INTERNAL_TOKEN,
        )

    def get_registration(self, payer_document: str, device_id: str) -> DeviceRegistryRecord:
        """O cadastro do par pagador–aparelho, do jeito que a fonte respondeu.

        O documento chega só com dígitos (utils/document_number.py). O id
        do aparelho é texto opaco, e vai escapado no caminho: um id com
        barra não pode virar outro endereço.
        """
        masked_document = mask_document(payer_document)
        device = quote(device_id, safe="")

        try:
            response = self.send(
                endpoint=f"/device-registration/{payer_document}/{device}",
                method="GET",
                log_as=f"/device-registration/{masked_document}/{device}",
            )
        except requests.RequestException as error:
            return self._unavailable(masked_document, f"request failed ({type(error).__name__})")

        if response.status == 404:
            return DeviceRegistryRecord(RegistrationStatus.UNCOVERED)
        if response.status != 200:
            return self._unavailable(masked_document, f"status {response.status}")

        body = response.json if isinstance(response.json, dict) else {}
        provenance = body.get("provenance")
        if provenance is not None and not isinstance(provenance, str):
            return self._unavailable(masked_document, "body with a provenance that is not text")

        registration_status = body.get("registration_status")
        if registration_status == "unregistered":
            return DeviceRegistryRecord(RegistrationStatus.UNREGISTERED, provenance=provenance)
        if registration_status != "registered":
            return self._unavailable(masked_document, "body without a known registration_status")

        # Cadastro sem data de início não tem vigência, e vigência é o que
        # separa cadastrado de revogado: resposta fora do combinado.
        try:
            registered_at = parse_instant(body.get("registered_at"))
            revoked_at = parse_instant(body.get("revoked_at"), optional=True)
        except ValueError:
            return self._unavailable(masked_document, "body with registration dates out of the agreed format")

        return DeviceRegistryRecord(RegistrationStatus.REGISTERED, registered_at, revoked_at, provenance)

    def _unavailable(self, masked_document: str, reason: str) -> DeviceRegistryRecord:
        # Toda desistência deixa uma linha dizendo por quê; pro
        # RestConnector, um corpo inválido foi um 200 como outro qualquer.
        self.logger.warning(f"DEVICE REGISTRY UNAVAILABLE {masked_document} - {reason}")
        return DeviceRegistryRecord(RegistrationStatus.UNAVAILABLE)
