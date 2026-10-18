"""Quem paga e quem recebe: identidades aleatórias, um mundo por teste.

O motor soma o histórico por documento, conta, aparelho e IP, e o banco
continua de pé entre um teste e outro — e entre uma rodada e a seguinte.
Com um CPF fixo, o quinto teste a rodar veria os Pix dos quatro anteriores
e o P04 dispararia sem ninguém pedir. Por isso cada teste cria as pessoas
dele, que ninguém mais usou, e nenhum teste precisa limpar o banco.

Um Payer leva documento, conta, aparelho e IP; um Recipient leva documento
e conta (com o ISPB). Os dois são reaproveitados entre os vários Pix de um
mesmo teste — é assim que se monta histórico.

O aparelho do Payer nasce cadastrado no banco: new_payer escreve no mock
do cadastro de aparelhos que o pagador está na fixture e que o aparelho
dele está registrado. É fixture da fonte, e entra pela fonte — a RFC manda
semear o cadastro por fixture, nunca por POST Pix. Qualquer outro
aparelho que o pagador use (new_device) responde "sem cadastro".
"""

from dataclasses import dataclass, replace
from datetime import datetime
from random import SystemRandom
from uuid import uuid4

from tests.utils.clock import MATURE_OPENING_DATE
from tests.utils.sources_mock import SourcesMock

# Desde quando o aparelho de sempre está cadastrado: 2020, antes de
# qualquer data que um cenário use. Cadastro tem vigência, e um cenário
# que precise de aparelho cadastrado ontem registra o dele pelo
# sources_mock.
DEVICE_REGISTERED_AT = MATURE_OPENING_DATE

_random = SystemRandom()


def new_cpf() -> str:
    """Onze dígitos, com os dígitos verificadores certos.

    O schema não confere o verificador (src/schemas/post_pix_transaction.json),
    mas documento de verdade não custa nada. Repare no _distinct_digits:
    base com todos os dígitos iguais é sorteada de novo — os documentos de
    dígitos repetidos (99999999999, 77777777777, 66666666666, 55555555555,
    88888888888) são os de demonstração do mock das fontes, e um teste
    que caísse num deles receberia o roteiro de outro.
    """
    digits = _distinct_digits(9)
    for _ in range(2):
        total = sum((len(digits) + 1 - position) * digit for position, digit in enumerate(digits))
        rest = total % 11
        digits.append(0 if rest < 2 else 11 - rest)
    return "".join(str(digit) for digit in digits)


def new_cnpj() -> str:
    """Catorze dígitos de uma matriz (0001), com os verificadores certos."""
    digits = _distinct_digits(8) + [0, 0, 0, 1]
    for _ in range(2):
        total = sum(digit * (position % 8 + 2) for position, digit in enumerate(reversed(digits)))
        check = 11 - total % 11
        digits.append(check if check < 10 else 0)
    return "".join(str(digit) for digit in digits)


def cpf_with_wrong_check_digit() -> str:
    """Um CPF com o último dígito trocado: formato certo, conta errada."""
    cpf = new_cpf()
    return cpf[:-1] + str((int(cpf[-1]) + 1) % 10)


def format_document(document: str) -> str:
    """O documento com a pontuação do desafio: 000.000.000-00 ou 00.000.000/0000-00."""
    if len(document) == 11:
        return f"{document[0:3]}.{document[3:6]}.{document[6:9]}-{document[9:11]}"
    return f"{document[0:2]}.{document[2:5]}.{document[5:8]}/{document[8:12]}-{document[12:14]}"


def only_digits(document: str) -> str:
    return "".join(character for character in document if character.isdigit())


def new_device() -> str:
    return f"dvc-{uuid4().hex[:16]}"


def new_ip() -> str:
    """Um IPv4 da faixa privada 10.0.0.0/8.

    São 16 milhões de endereços, e a chance de dois testes sortearem o
    mesmo é desprezível.
    """
    return f"10.{_random.randrange(256)}.{_random.randrange(256)}.{_random.randrange(1, 255)}"


def new_ipv6() -> str:
    """Um IPv6 da faixa de documentação 2001:db8::/32, em minúsculas."""
    groups = ":".join(f"{_random.randrange(1, 0x10000):x}" for _ in range(4))
    return f"2001:db8:{groups}::1"


def new_participant() -> str:
    """Um ISPB, o código de oito dígitos do banco de destino."""
    return f"{_random.randrange(10**8):08d}"


@dataclass(frozen=True)
class Account:
    """Uma conta.

    participant é o ISPB: None na conta de origem, que o contrato manda
    sem ele.
    """

    branch: str
    account_number: str
    opening_date: datetime
    participant: str | None = None


def new_account(opening_date: datetime = MATURE_OPENING_DATE, participant: str | None = None) -> Account:
    # Dez dígitos de conta e quatro de agência: a chance de dois testes
    # baterem na mesma conta sem querer é desprezível.
    return Account(
        branch=f"{_random.randrange(1, 10000):04d}",
        account_number=str(_random.randrange(10**9, 10**10)),
        opening_date=opening_date,
        participant=participant,
    )


@dataclass(frozen=True)
class Payer:
    """Quem paga: o documento, a conta de origem e o ambiente de sempre.

    O ambiente é o aparelho e o IP. Um Pix de outro aparelho é o mesmo
    Payer com o device_id trocado no payload.
    """

    document: str
    account: Account
    device_id: str
    ip: str


@dataclass(frozen=True)
class Recipient:
    """Quem recebe: o titular (documento) e a conta de destino."""

    document: str
    account: Account

    def with_new_account(self, opening_date: datetime = MATURE_OPENING_DATE) -> "Recipient":
        """O mesmo titular noutra conta, noutro banco.

        R01 olha a idade da CONTA; R02 e R03 somam o TITULAR por todas as
        contas dele.
        """
        return replace(self, account=new_account(opening_date, participant=new_participant()))


def new_payer(
    opening_date: datetime = MATURE_OPENING_DATE,
    document: str | None = None,
    ip: str | None = None,
    covered: bool = True,
) -> Payer:
    """Um pagador novo, coberto pelo cadastro de aparelhos e com o aparelho de sempre cadastrado.

    covered=False deixa o pagador fora da fixture do cadastro: a fonte
    responde 404 pra qualquer aparelho dele, e o P03 sai unavailable —
    que não é "sem cadastro". O que new_payer combina com o mock fica até
    vencer a validade (5 min): é cadastro, e precisa durar o teste.
    """
    payer = Payer(
        document=document or new_cpf(),
        account=new_account(opening_date),
        device_id=new_device(),
        ip=ip or new_ip(),
    )
    if covered:
        registry = SourcesMock()
        registry.cover_payer(payer.document)
        registry.register_device(payer.document, payer.device_id, registered_at=DEVICE_REGISTERED_AT)
    return payer


def new_recipient(
    opening_date: datetime = MATURE_OPENING_DATE,
    document: str | None = None,
    company: bool = False,
) -> Recipient:
    if document is None:
        document = new_cnpj() if company else new_cpf()
    return Recipient(document=document, account=new_account(opening_date, participant=new_participant()))


def _distinct_digits(count: int) -> list[int]:
    while True:
        digits = [_random.randrange(10) for _ in range(count)]
        if len(set(digits)) > 1:
            return digits
