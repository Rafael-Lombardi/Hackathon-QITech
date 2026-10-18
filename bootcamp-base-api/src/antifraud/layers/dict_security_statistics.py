"""Camada DICT01 — estatística de segurança do DICT."""

from antifraud import policy
from antifraud.contract import Context, Signal


class DictSecurityStatisticsLayer:
    """Avalia a estatística de segurança retornada pelo DICT."""

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
