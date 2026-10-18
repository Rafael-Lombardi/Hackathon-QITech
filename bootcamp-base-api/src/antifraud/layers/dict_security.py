"""Camada 3 — as marcações de fraude do DICT: DICT02 e DICT03.

O DICT guarda, além das estatísticas, as marcações de fraude que os
participantes registram contra uma chave ou um documento (Manual
Operacional do DICT, cap. 10 e 18): a suspeita, aberta por quem
notificou, e a confirmada. Elas vêm na mesma consulta que traz a
estatística do DICT01, e o controller entrega as duas no Context.

Sem resposta do DICT, os dois sinais saem unavailable. Resposta sem
marcação é not_triggered: aí o DICT olhou e não tinha nada contra o
documento, e isso é informação, não falta dela.
"""

from antifraud import policy
from antifraud.contract import Context, FraudMarker, Signal


class DictSecurityLayer:
    def evaluate(self, context: Context) -> list[Signal]:
        marker = context.dict_fraud_marker
        if marker is None:
            return [policy.unavailable("DICT02"), policy.unavailable("DICT03")]
        return [
            _marker_signal("DICT02", marker, FraudMarker.SUSPICIOUS),
            _marker_signal("DICT03", marker, FraudMarker.CONFIRMED),
        ]


def _marker_signal(code: str, marker: FraudMarker, expected: FraudMarker) -> Signal:
    if marker is expected:
        return policy.triggered(code, {"fraud_marker": marker.value})
    return policy.not_triggered(code)
