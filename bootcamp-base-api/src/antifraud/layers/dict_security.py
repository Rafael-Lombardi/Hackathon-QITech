"""Camada 3, segunda parte — as ocorrências de segurança do DICT sobre o recebedor: DICT02, DICT03 e DICT04.

O DICT guarda, além das estatísticas, as marcações de fraude que os
participantes registram contra uma chave ou um documento (Manual
Operacional do DICT, cap. 10 e 18). A RFC as separa em três tipos (§6), e
cada tipo é um sinal: a evidência ligada a fraude (DICT02), a fraude
confirmada na fixture (DICT03) e a denúncia ainda não resolvida (DICT04).
O Context já traz só as ocorrências vigentes no instante da avaliação,
deduplicadas; um sinal dispara se há alguma do tipo dele, e a evidência
lista os ids. Os três dividem um teto (dict_security): mais ocorrências
não pesam além da mais grave.

Sem resposta do DICT sobre o recebedor, os três saem unavailable.
Resposta sem ocorrência vigente é not_triggered: aí o DICT olhou e não
tinha nada contra o documento, e isso é informação, não falta dela — mas
não é atestado: "clear" não comprova segurança (RFC §3.1). As
ocorrências contra o PAGADOR não passam por aqui: não pontuam, e só
abrem a porteira do D-REJ-PAY, no engine.
"""

from antifraud import policy
from antifraud.contract import Context, DictSecurityEvidence, OccurrenceKind, Signal

_SIGNAL_KINDS = (
    ("DICT02", OccurrenceKind.FRAUD_RELATED_EVIDENCE),
    ("DICT03", OccurrenceKind.CONFIRMED_IN_MOCK),
    ("DICT04", OccurrenceKind.UNRESOLVED_REPORT),
)


class DictSecurityLayer:
    def evaluate(self, context: Context) -> list[Signal]:
        evidence = context.recipient_dict
        if evidence is None:
            return [policy.unavailable(code) for code, _ in _SIGNAL_KINDS]
        return [_occurrence_signal(code, kind, evidence) for code, kind in _SIGNAL_KINDS]


def _occurrence_signal(code: str, kind: OccurrenceKind, evidence: DictSecurityEvidence) -> Signal:
    occurrences = evidence.occurrences_of(kind)
    if occurrences:
        return policy.triggered(code, {"occurrences": [occurrence.occurrence_id for occurrence in occurrences]})
    return policy.not_triggered(code)
