"""CPF e CNPJ: o documento como chega no payload, e como ele segue daqui.

O mesmo CPF pode chegar de dois jeitos — "123.456.789-09" e "12345678909"
— e o JSON Schema aceita os dois. Pro histórico, os dois precisam ser UM:
toda regra por pessoa agrupa pelo documento, e se a máscara entrasse na
chave, o mesmo pagador viraria duas identidades e cada contagem enxergaria
metade. Por isso o documento vira só dígitos uma vez, logo depois do
schema, e é assim que ele chega ao banco, às consultas e ao DICT, o
diretório de chaves Pix do Banco Central.

O is_valid_cpf que o Base Service tinha aqui não voltou: o contrato aceita
o documento sem conferir os dígitos verificadores, e o porquê está no
$comment de document_number, em src/schemas/post_pix_transaction.json.
"""

import re

# Dígito é de 0 a 9 e mais nada. Repare que um linter de editor (o
# SonarLint, regra S6353) sugere \D no lugar de [^0-9], e erra: no Python,
# \d é qualquer algarismo Unicode, e o "٣" arábico passaria daqui pro CHECK
# da tabela account, que o recusa com um 500. O str.isdigit() cai na mesma
# armadilha.
NON_DIGIT = re.compile("[^0-9]")


def normalize_document(value: str) -> str:
    """O documento só com os dígitos: 11 num CPF, 14 num CNPJ."""
    return NON_DIGIT.sub("", value)


def mask_document(value: str) -> str:
    """O documento pronto pro log, sem nunca aparecer inteiro.

    Ficam à vista os seis dígitos do meio: ***.456.789-** num CPF e
    **.345.678/****-** num CNPJ, tenha o valor chegado como "123.456.789-09"
    ou como "12345678909". É o recorte que os portais de transparência do
    governo usam pra publicar CPF: dá pra reconhecer um cliente numa
    investigação, e um log vazado não entrega o CPF inteiro de ninguém.

    Valor de outro tamanho, ou que nem é texto, não deveria chegar aqui. Se
    chegar, sai "***": na dúvida, o log perde a informação em vez de vazar o
    documento — e quem só queria escrever uma linha de log não quebra por
    causa dela.
    """
    if not isinstance(value, str):
        return "***"

    digits = normalize_document(value)

    if len(digits) == 11:
        return f"***.{digits[3:6]}.{digits[6:9]}-**"

    if len(digits) == 14:
        return f"**.{digits[2:5]}.{digits[5:8]}/****-**"

    return "***"
