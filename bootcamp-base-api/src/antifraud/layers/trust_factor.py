"""Camada 3 — confiabilidade do destino, pelo trust factor do DICT.

O DICT é o diretório do Banco Central que liga chave Pix a conta. Aqui
ele é um mock — ficção aceita pelo time — que devolve, por documento,
uma nota de 0 a 100, maior pra mais confiável. Quem consulta é o
controller, pelo connector, antes de montar o Context; esta camada só lê
o número, e só o do recebedor.

Ela sobe ou confirma, e nunca rebaixa: existe o sinal de confiança baixa
(DICT01) e não existe o de confiança alta com ponto negativo — uma nota
boa lá fora não apaga o que o histórico daqui viu. Consulta que falhou
(timeout, 500, resposta torta) chega como None e vira unavailable: o Pix
segue decidido pelas outras duas camadas, sem ponto e sem desconto.
"""

from antifraud import policy
from antifraud.contract import Context, Signal


class TrustFactorLayer:
    def evaluate(self, context: Context) -> list[Signal]:
        return [_dict01_low_trust(context)]


def _dict01_low_trust(context: Context) -> Signal:
    trust_factor = context.trust_factor
    if trust_factor is None:
        return policy.unavailable("DICT01")
    if trust_factor < policy.LOW_TRUST_LIMIT:
        return policy.triggered("DICT01", {"trust_factor": trust_factor, "threshold": policy.LOW_TRUST_LIMIT})
    return policy.not_triggered("DICT01")
