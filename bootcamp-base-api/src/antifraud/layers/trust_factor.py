"""Camada 3 — estatísticas de segurança do DICT.

O DICT é o diretório do Banco Central que liga chave Pix a conta. O
    real não devolve nota: devolve contadores por CPF ou CNPJ e notificações
    de infração por tipo de fraude, com até 12 h de atraso. O indicador
    numérico é somente simplificação do mock. Quem
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
    statistics = context.dict_security_statistics
    if statistics is None:
        return policy.unavailable("DICT01")
    if statistics < policy.LOW_TRUST_LIMIT:
        return policy.triggered("DICT01", {"security_statistics": statistics, "threshold": policy.LOW_TRUST_LIMIT})
    return policy.not_triggered("DICT01")
