"""Camada 3, primeira parte — a estatística de segurança do DICT sobre o recebedor (DICT01).

Abaixo de 30 dispara. Sem resposta do DICT sobre o recebedor (fora do
ar, lento ou respondendo fora do combinado) é unavailable, e nunca
not_triggered: falta de resposta não é nota boa. O porquê do peso e do
teto próprio está no catálogo do policy.py; as ocorrências, que vêm na
mesma resposta, ficam na segunda parte (dict_security.py).
"""

from antifraud import policy
from antifraud.contract import Context, Signal


class DictSecurityStatisticsLayer:
    def evaluate(self, context: Context) -> list[Signal]:
        evidence = context.recipient_dict
        if evidence is None:
            return [policy.unavailable("DICT01")]
        if evidence.statistics < policy.LOW_SECURITY_STATISTICS_LIMIT:
            return [policy.triggered(
                "DICT01",
                {"security_statistics": evidence.statistics, "threshold": policy.LOW_SECURITY_STATISTICS_LIMIT},
            )]
        return [policy.not_triggered("DICT01")]
