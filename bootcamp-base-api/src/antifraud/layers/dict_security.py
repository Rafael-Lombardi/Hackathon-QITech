"""Evidência antifraude do DICT, separada de score interno do banco."""

from antifraud import policy
from antifraud.contract import Context, Signal


class DictSecurityLayer:
    """Mapeia marcações DICT simuladas em sinais auditáveis."""

    def evaluate(self, context: Context) -> list[Signal]:
        assessment = context.recipient_dict_security_assessment
        if assessment is None:
            return [policy.unavailable("DICT02"), policy.unavailable("DICT03")]
        if assessment == "suspicious":
            return [policy.triggered("DICT02", {"assessment": assessment}), policy.not_triggered("DICT03")]
        if assessment == "confirmed_in_mock":
            return [policy.not_triggered("DICT02"), policy.triggered("DICT03", {"assessment": assessment})]
        if assessment == "unavailable":
            return [policy.unavailable("DICT02"), policy.unavailable("DICT03")]
        return [policy.not_triggered("DICT02"), policy.not_triggered("DICT03")]
