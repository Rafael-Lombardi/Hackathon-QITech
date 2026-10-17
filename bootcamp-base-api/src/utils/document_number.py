"""CPF e CNPJ: o documento como chega no payload, e como ele segue daqui.

O mesmo CPF pode chegar de dois jeitos — "123.456.789-09" e "12345678909"
— e o JSON Schema aceita os dois. Pro histórico, os dois precisam ser UM:
toda regra por pessoa agrupa pelo documento, e se a máscara entrasse na
chave, o mesmo pagador viraria dois customers e cada contagem enxergaria
metade. Por isso o documento vira só dígitos uma vez, logo depois do
schema, e é assim que ele chega ao banco, às consultas e ao DICT.

Não há conferência de dígito verificador aqui, e é de propósito: o
pagador é cliente nosso e o recebedor veio do DICT, os dois já conferidos
na origem — e um CPF sintético de teste não pode virar 400.
"""

import re

# Dígito é de 0 a 9 e mais nada. Repare que o linter sugere \D no lugar de
# [^0-9], e erra: no Python, \d é qualquer algarismo Unicode, e o "٣"
# arábico passaria daqui pro CHECK da tabela customer, que o recusa com um
# 500. O str.isdigit() cai na mesma armadilha.
NON_DIGIT = re.compile("[^0-9]")


def normalize_document(value: str) -> str:
    """O documento só com os dígitos: 11 num CPF, 14 num CNPJ."""
    return NON_DIGIT.sub("", value)


def mask_document(value: str) -> str:
    """O documento pronto pro log, sem nunca aparecer inteiro.

    Ficam à vista os seis dígitos do meio, na máscara de sempre:
    ***.456.789-** num CPF e **.345.678/****-** num CNPJ, tenha o valor
    chegado com máscara ou sem. É o recorte que os portais de transparência
    do governo usam pra publicar CPF — mostra o bastante pra reconhecer um
    cliente numa investigação, e esconde o bastante para que o log não vire
    uma lista de CPFs.

    Valor de outro tamanho não deveria chegar aqui, porque o schema barra
    antes. Se chegar, sai todo em asterisco: na dúvida, o log perde
    informação, e nunca o contrário.
    """
    digits = normalize_document(value)

    if len(digits) == 11:
        return f"***.{digits[3:6]}.{digits[6:9]}-**"

    if len(digits) == 14:
        return f"**.{digits[2:5]}.{digits[5:8]}/****-**"

    return "***"
