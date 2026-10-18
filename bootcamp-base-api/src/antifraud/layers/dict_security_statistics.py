"""Camada 3, primeira parte — a estatística de segurança do DICT (DICT01).

Abaixo de 30 dispara. None (DICT fora do ar, lento ou respondendo fora
do combinado) é unavailable, e nunca not_triggered: falta de resposta
não é nota boa. O porquê do peso e do teto próprio está no catálogo do
policy.py; a marcação de fraude, que vem na mesma resposta, fica na
segunda parte (dict_security.py).
"""

from antifraud import policy
from antifraud.contract import Context, Signal


class DictSecurityStatisticsLayer:
    def evaluate(self, context: Context) -> list[Signal]:
        statistics = context.dict_security_statistics
        if statistics is None:
            return [policy.unavailable("DICT01")]
        if statistics < policy.LOW_SECURITY_STATISTICS_LIMIT:
            return [policy.triggered(
                "DICT01",
                {"security_statistics": statistics, "threshold": policy.LOW_SECURITY_STATISTICS_LIMIT},
            )]
        return [policy.not_triggered("DICT01")]
