"""Camada 3 — confiabilidade do destino, pelo trust factor do DICT.

O DICT é o diretório do Banco Central que liga chave Pix a conta. O
real não devolve nota: devolve contadores por CPF ou CNPJ, como
liquidações recebidas e notificações de infração por tipo de fraude
(conta laranja, conta de fraudador), com até 12 h de atraso (Manual do
DICT, cap. 18). A nota de 0 a 100, maior pra mais confiável, é a
simplificação combinada no time, e quem responde é um mock. Quem
consulta é o controller, pelo connector, antes de montar o Context;
esta camada só lê o número.

Só o recebedor é consultado. A pergunta da família Contraparte é se
quem recebe é confiável; o pagador é cliente nosso, e o histórico daqui
já fala dele.

A camada sobe o risco do destino e nunca baixa: não existe sinal de
confiança alta com ponto negativo. Consulta que falhou (timeout, 500,
resposta torta) chega como None e vira unavailable, e o Pix segue
decidido pelas outras duas camadas, sem ponto e sem desconto.
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
