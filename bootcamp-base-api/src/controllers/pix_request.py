"""O Pix que chegou, já com os tipos certos: a etapa 1 do controller.

É trabalho de controller — converter o que o schema não sabe, como o
_parse_birthdate do Base Service fazia — e mora num arquivo à parte só
pelo tamanho. Não foi pra utils/, que guarda ferramenta de uso geral:
isto aqui serve a uma rota só.

O JSON Schema confere a FORMA do payload e para por aí. Depois dele, a data
ainda é texto, o documento pode vir com máscara, o mesmo IPv6 pode vir
escrito de dois jeitos e o valor pode ser 150000.0, que o schema conta
como integer. Este arquivo converte tudo isso uma vez, e daqui pra frente
o serviço só enxerga o PixRequest.

Aqui não mora limite nenhum. Tamanho, faixa e formato ficam no schema, que
é o contrato; esta conversão só recusa o que o schema não tem como saber —
que fevereiro não tem dia 31, por exemplo. Medir em dois lugares já deu
errado neste projeto. No 9d12cad, o schema aceitava participant em
source_account e o modelo Pydantic, com extra="forbid", recusava: o
contrato dizia sim e a API respondia 400. E o format: date-time do schema
não conferia nada — quem validava a data era só o Pydantic, e o contrato
prometia uma checagem que ele mesmo não fazia.

Idade das contas e hora local também ficam de fora. São cálculos do
controller e do motor, feitos sobre o PixRequest pronto.
"""

import ipaddress
import math
from dataclasses import dataclass, field
from datetime import datetime

from errors import InvalidSchema
from utils.document_number import normalize_document


@dataclass(frozen=True)
class AccountData:
    """Uma das duas contas do Pix.

    participant é o ISPB e só existe no destino: o contrato não traz o da
    origem, e aqui ele vem None. document_number é o do titular, já só com
    dígitos, e fica fora do repr pra um log descuidado não levar CPF
    inteiro.
    """

    participant: str | None
    branch: str
    account_number: str
    opening_date: datetime  # com fuso
    document_number: str = field(repr=False)


@dataclass(frozen=True)
class PixRequest:
    """O payload do POST /pix/transaction, convertido.

    external_id é o id do payload; o nome é o da coluna, e evita confundir
    com o id da linha no banco. As datas têm fuso, amount é int em
    centavos, ip vem na grafia canônica, e latitude e longitude vêm as duas
    ou nenhuma.

    payload é o dicionário que chegou, intacto: é ele que vai para
    pix_transaction_data, e é com ele que um reenvio é comparado. Repare
    que lá dentro o documento continua como veio, com máscara se ela veio —
    quem precisa do documento lê source_account.document_number, nunca o
    payload. Fica fora do repr pelo mesmo motivo do documento.
    """

    external_id: str
    amount: int
    transaction_date: datetime
    source_account: AccountData
    destination_account: AccountData
    device_id: str
    channel: str
    ip: str
    latitude: float | None
    longitude: float | None
    payload: dict = field(repr=False)


def parse_pix_request(payload: dict) -> PixRequest:
    """Converte um payload que JÁ passou pelo SchemaHandler.

    Antes do schema não serve: aqui ninguém confere campo obrigatório, e um
    payload cru estoura num KeyError, que vira 500.
    """
    source = payload["source"]
    geolocation = source.get("geolocation")

    if geolocation is None:
        latitude = None
        longitude = None
    else:
        latitude = _parse_coordinate(geolocation["latitude"], "source.geolocation.latitude")
        longitude = _parse_coordinate(geolocation["longitude"], "source.geolocation.longitude")

    return PixRequest(
        external_id=payload["id"],
        amount=int(payload["amount"]),
        transaction_date=_parse_date_time(payload["transaction_date"], "transaction_date"),
        source_account=_parse_account(payload["source_account"], "source_account"),
        destination_account=_parse_account(payload["destination_account"], "destination_account"),
        device_id=source["device_id"],
        channel=source["channel"],
        ip=_parse_ip(source["ip"], "source.ip"),
        latitude=latitude,
        longitude=longitude,
        payload=payload,
    )


def _parse_account(account: dict, path: str) -> AccountData:
    return AccountData(
        participant=account.get("participant"),
        branch=account["branch"],
        account_number=account["account_number"],
        opening_date=_parse_date_time(account["opening_date"], f"{path}.opening_date"),
        document_number=normalize_document(account["owner"]["document_number"]),
    )


def _parse_date_time(value: str, path: str) -> datetime:
    """A data com fuso, ou 400 — nunca 500.

    O schema já garantiu a forma e o fuso. O que um pattern não sabe é
    quantos dias tem fevereiro — ele só conta caracteres. Por isso
    "2026-02-31T10:00:00-03:00" chega aqui inteiro, e é aqui que ele para.

    400, e não o 422 que o Base Service dava pra data de nascimento
    impossível: 31 de fevereiro nem é um date-time da RFC 3339, e o format:
    date-time recusaria esse texto se o pacote rfc3339-validator estivesse
    instalado. É erro de forma, não de mundo.
    """
    try:
        return datetime.fromisoformat(value)
    except ValueError as error:
        raise InvalidSchema(f"{value!r} is not a valid date-time in {path}: {error}") from error


def _parse_ip(value: str, path: str) -> str:
    """O IP na grafia canônica do ipaddress: IPv6 comprimido, em minúsculas.

    "2001:DB8::1" e "2001:db8:0:0:0:0:0:1" são o mesmo endereço, e o
    histórico compara texto: sem isto, o mesmo cliente na mesma rede
    ganharia um P09 de "IP nunca visto" só por mudar a grafia. O payload
    continua com a grafia que chegou — é o cru que o reenvio compara.

    O schema já conferiu o endereço. O except fica pro dia em que o validate
    perder o format_checker: aí o 400 continua 400.
    """
    try:
        return ipaddress.ip_address(value).compressed
    except ValueError as error:
        raise InvalidSchema(f"{value!r} is not a valid IP address in {path}") from error


def _parse_coordinate(value: float, path: str) -> float:
    """A coordenada como float, recusando NaN.

    O json do Python aceita NaN, que nem é JSON, e o schema não tem como
    barrar: toda comparação com NaN dá falso, e ele passa pelo minimum e
    pelo maximum sem esbarrar em nenhum. Mais adiante, o CHECK de faixa da
    tabela pix_transaction recusaria — com um 500.
    """
    coordinate = float(value)

    if not math.isfinite(coordinate):
        raise InvalidSchema(f"{coordinate!r} is not a valid number in {path}")

    return coordinate
