from dataclasses import dataclass

import requests

from antifraud.contract import DictOccurrence, OccurrenceKind
from connectors.rest_connector import RestConnector, parse_instant
from constants import DICT_API_URL, SOURCES_INTERNAL_TOKEN, SOURCES_TIMEOUT
from errors import DictUnavailable
from utils.document_number import mask_document

_KINDS_BY_TEXT = {kind.value: kind for kind in OccurrenceKind}


@dataclass(frozen=True)
class DictSecurityInformation:
    """O que o DICT diz de um documento: a estatística de segurança e as ocorrências, como vieram.

    As ocorrências vêm todas, vigentes ou não: quem sabe o instante da
    avaliação é o controller, e é ele que filtra. Já deduplicadas por
    (source, occurrence_id), na ordem em que o DICT as mandou.
    """

    statistics: int  # 0 a 100
    occurrences: tuple[DictOccurrence, ...]


class DictConnector(RestConnector):
    """Fala com o DICT do Banco Central — aqui, com o mock dele.

    DICT é o Diretório de Identificadores de Contas Transacionais, o
    cadastro do Banco Central que liga chave Pix a conta. O de verdade
    devolve estatísticas de segurança e marcações de fraude; o mock reduz
    as estatísticas a um número e traz as marcações como ocorrências
    tipadas e datadas, no mesmo corpo (RFC §6). É a parte de I/O da
    camada 3 do motor, e quem faz o papel do DICT é o mock do
    docker-compose.yml (mockserver/README.md).

    Repare no que este connector NÃO decide: o que fazer quando o DICT
    falha. Prazo estourado, ninguém atendendo, status ou corpo fora do
    combinado — tudo vira DictUnavailable, e mais nada. O que isso
    significa pro Pix, quem decide é o controller. O combinado é 200 com
    a estatística (inteiro de 0 a 100) e, opcional, a lista occurrences,
    pra qualquer documento; o resto, 404 incluído, é fora do combinado.
    Dentro da lista a régua é outra: uma ocorrência torta — sem prazo,
    de tipo que não conhecemos, sem id — é ignorada e vai pro log, e as
    outras valem. A lista é de evidências, e uma evidência que não
    entendemos não apaga as que entendemos.
    """

    def __init__(self) -> None:
        super().__init__(
            class_name=__name__,
            base_url=DICT_API_URL,
            timeout=SOURCES_TIMEOUT,
            internal_token=SOURCES_INTERNAL_TOKEN,
        )

    def get_security_information(self, document_number: str) -> DictSecurityInformation:
        """A estatística de segurança e as ocorrências de um documento.

        O documento chega só com dígitos: quem normaliza é o
        utils/document_number.py, antes daqui. Qualquer falha levanta
        DictUnavailable.
        """
        masked_document = mask_document(document_number)

        # O `from None` deixa o erro do requests aqui dentro. Lá fora ele
        # não faz falta — o log já contou o que houve — e a mensagem dele
        # repete o endereço inteiro, documento junto.
        try:
            response = self.send(
                endpoint=f"/security-statistics/{document_number}",
                method="GET",
                log_as=f"/security-statistics/{masked_document}",
            )
        except requests.RequestException as error:
            raise self._unavailable(masked_document, f"request failed ({type(error).__name__})") from None

        if response.status != 200:
            raise self._unavailable(masked_document, f"status {response.status}")

        body = response.json if isinstance(response.json, dict) else {}
        security_statistics = body.get("security_statistics")

        # Repare no `type(...) is int` no lugar do isinstance: em Python,
        # True é um int que vale 1, e um {"security_statistics": true} passaria
        # como estatística DICT 1. O 70.0 e o "70" também ficam de fora — o
        # combinado é um inteiro, e resposta fora do combinado não vira
        # número por boa vontade nossa.
        if type(security_statistics) is not int or not 0 <= security_statistics <= 100:
            raise self._unavailable(masked_document, "body without integer DICT security statistics")

        # Sem ocorrências, o DICT manda a lista vazia ou nem manda a chave.
        # Qualquer coisa que não seja lista é corpo fora do combinado.
        raw_occurrences = body.get("occurrences", [])
        if raw_occurrences is None:
            raw_occurrences = []
        if not isinstance(raw_occurrences, list):
            raise self._unavailable(masked_document, "body with occurrences that are not a list")

        return DictSecurityInformation(
            statistics=security_statistics,
            occurrences=self._occurrences(masked_document, raw_occurrences),
        )

    def _occurrences(self, masked_document: str, raw_occurrences: list) -> tuple[DictOccurrence, ...]:
        occurrences: dict[tuple[str, str], DictOccurrence] = {}
        for raw in raw_occurrences:
            occurrence = self._occurrence(masked_document, raw)
            # Deduplicação por (source, occurrence_id): a mesma ocorrência
            # mandada duas vezes conta uma, e vale a primeira (RFC §6).
            if occurrence is not None:
                occurrences.setdefault((occurrence.source, occurrence.occurrence_id), occurrence)
        return tuple(occurrences.values())

    def _occurrence(self, masked_document: str, raw) -> DictOccurrence | None:
        if not isinstance(raw, dict):
            return self._ignored(masked_document, "occurrence that is not an object")
        occurrence_id, source = raw.get("occurrence_id"), raw.get("source")
        if not isinstance(occurrence_id, str) or not occurrence_id or not isinstance(source, str) or not source:
            return self._ignored(masked_document, "occurrence without id or source")
        kind = _KINDS_BY_TEXT.get(raw.get("kind"))
        if kind is None:
            return self._ignored(masked_document, f"occurrence {occurrence_id} of unknown kind")
        try:
            reported_at = parse_instant(raw.get("reported_at"))
            expires_at = parse_instant(raw.get("expires_at"))
        except ValueError:
            return self._ignored(masked_document, f"occurrence {occurrence_id} without a valid validity period")
        severity, fixture_version = raw.get("severity"), raw.get("fixture_version")
        return DictOccurrence(
            occurrence_id=occurrence_id,
            kind=kind,
            reported_at=reported_at,
            expires_at=expires_at,
            source=source,
            severity=severity if isinstance(severity, str) else None,
            fixture_version=fixture_version if isinstance(fixture_version, str) else None,
        )

    def _ignored(self, masked_document: str, reason: str) -> None:
        # Fica no log, e não vira unavailable: as outras ocorrências da
        # resposta continuam valendo.
        self.logger.warning(f"DICT OCCURRENCE IGNORED {masked_document} - {reason}")
        return None

    def _unavailable(self, masked_document: str, reason: str) -> DictUnavailable:
        # Toda desistência deixa uma linha dizendo por quê. Sem ela, o
        # corpo inválido nem apareceria no log: pro RestConnector, foi
        # um 200 como outro qualquer.
        self.logger.warning(f"DICT UNAVAILABLE {masked_document} - {reason}")
        return DictUnavailable(reason)
