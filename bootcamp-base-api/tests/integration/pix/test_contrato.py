"""O contrato HTTP do Pix: o que entra, o que sai, e as recusas.

POST /pix/transaction responde 201 com a análise nova, 200 no reenvio
idêntico (a mesma análise, sem contar de novo), 400 fora do schema, 403
sem token e 409 quando o mesmo id volta com outro conteúdo. GET
/pix/transaction/{id} devolve a análise gravada, ou 404. O corpo de uma
análise é {key, status, metadata}, o mesmo no POST e no GET.

Aqui moram dois testes do cenário 15 da RFC (reenvio e 409); os de
concorrência estão em test_concorrencia.py.
"""

import copy
import json
from datetime import timedelta
from uuid import UUID, uuid4

import pytest

from tests.utils import INTERNAL_TOKEN
from tests.utils.clock import T0, at, minutes_before
from tests.utils.identities import cpf_with_wrong_check_digit, format_document, new_cpf, new_payer, new_recipient
from tests.utils.pix import get_pix, pix_payload, post_pix, post_raw, seed, seed_pix
from tests.utils.requisition import ClientRequisition

# O catálogo como a resposta o mostra: código → (camada, família, eixo,
# granularidade, pontos quando dispara). É o CATALOG de
# src/antifraud/policy.py, na mesma ordem. Granularidade é a chave que a
# regra consultou: a conta de origem, o documento do pagador, o aparelho,
# a conta de destino, o documento do recebedor, o par... A RFC v0.5 pede
# que cada sinal diga qual.
CATALOG = {
    "P01": (1, "Perfil", "payer", "source_account", 10),
    "L01": (1, "Limite", "payer", "transaction", 8),
    "R01": (1, "Contraparte", "recipient", "destination_account", 12),
    "REL03": (1, "Contraparte", "recipient", "pair", -5),
    "L02": (2, "Limite", "payer", "payer", 10),
    "P03": (2, "Perfil", "payer", "payer", 18),
    "P04": (2, "Velocity", "payer", "payer", 20),
    "P05": (2, "Montante", "payer", "payer", 15),
    "P06": (2, "Perfil", "payer", "payer", 18),
    "P07": (2, "Velocity", "payer", "payer", 18),
    "P08": (2, "Perfil", "payer", "device", 12),
    "P09": (2, "Perfil", "payer", "payer", 6),
    "P10": (2, "Perfil", "payer", "payer", 12),
    "R02": (2, "Velocity", "recipient", "recipient", 16),
    "R03": (2, "Contraparte", "recipient", "recipient", 20),
    "R04": (2, "Contraparte", "recipient", "destination_account+recipient", 15),
    "REL01": (2, "Contraparte", "recipient", "pair", 8),
    "REL02": (2, "Contraparte", "recipient", "pair", -8),
    "DICT01": (3, "Contraparte", "recipient", "recipient", 32),
}

SIGNAL_KEYS = {"code", "layer", "family", "axis", "granularity", "status", "points"}

PIX_STATUSES = {"approved", "rejected", "two_factor_required"}
SUFFICIENCIES = {"unavailable", "low", "medium", "high"}
ERROR_KEYS = {"title", "description", "translation", "code"}

# O teto de tamanho do corpo do POST, 16 KiB.
MAX_BODY_BYTES = 16 * 1024


def challenge_example() -> dict:
    """O payload de exemplo do desafio, trocando só o id e os dois documentos.

    Contas, aparelho e IP ficam como no PDF, e se repetem a cada rodada
    da suíte. Os documentos não podem: com os do exemplo, a segunda
    rodada veria o Pix da primeira como histórico do mesmo pagador.
    """
    return {
        "id": f"pix-{uuid4().hex}",
        "amount": 150000,
        "transaction_date": "2026-10-17T22:15:00-03:00",
        "source_account": {
            "branch": "0001",
            "account_number": "88213",
            "opening_date": "2025-03-02T10:00:00-03:00",
            "owner": {"document_number": format_document(new_cpf())},
        },
        "destination_account": {
            "participant": "60701190",
            "branch": "3675",
            "account_number": "10442",
            "opening_date": "2026-10-10T09:00:00-03:00",
            "owner": {"document_number": format_document(new_cpf())},
        },
        "source": {
            "device_id": "dvc-9f2c71a8",
            "channel": "app",
            "ip": "198.51.100.23",
            "geolocation": {"latitude": -23.56, "longitude": -46.65},
        },
    }


def _replace(*path, value):
    """Troca o campo em `path` (chaves aninhadas) por `value`."""
    def apply(payload):
        target = payload
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value
    return apply


def _remove(*path):
    """Apaga o campo em `path`."""
    def apply(payload):
        target = payload
        for key in path[:-1]:
            target = target[key]
        del target[path[-1]]
    return apply


def _reversed_keys(value):
    """O mesmo JSON, com as chaves de todo objeto na ordem inversa."""
    if isinstance(value, dict):
        return {key: _reversed_keys(value[key]) for key in reversed(list(value))}
    return value


def _in_pieces(payload: dict):
    """O payload em pedaços, por um gerador: o requests manda chunked, sem Content-Length."""
    body = json.dumps(payload).encode()

    def pieces():
        yield body[:100]
        yield body[100:]

    return pieces()


# Cada caso quebra uma regra de src/schemas/post_pix_transaction.json e
# mantém o resto certo: quando um ficar vermelho, o id diz qual regra
# parou de ser cobrada.
OUT_OF_CONTRACT = [
    pytest.param(lambda payload: payload.clear(), id="objeto-vazio"),
    pytest.param(_replace("extra", value=1), id="campo-extra-na-raiz"),
    pytest.param(_replace("source", "imei", value="35-209900-176148-1"), id="campo-extra-em-source"),
    pytest.param(_remove("source"), id="sem-source"),
    pytest.param(_remove("destination_account", "participant"), id="participant-ausente"),
    pytest.param(_replace("destination_account", "participant", value="6070119"), id="participant-com-7-digitos"),
    pytest.param(_replace("source_account", "participant", value="60701190"), id="participant-na-conta-de-origem"),
    pytest.param(_replace("source_account", "owner", "document_number", value="123456789012"), id="documento-com-12-digitos"),
    pytest.param(_replace("source_account", "owner", "document_number", value="123.456.789-0X"), id="documento-com-letra"),
    pytest.param(_replace("destination_account", "owner", "document_number", value="123.456.78909"), id="documento-com-meia-pontuacao"),
    pytest.param(_replace("transaction_date", value="2026-10-14T14:00:00"), id="data-sem-fuso"),
    pytest.param(_replace("source_account", "opening_date", value="2025-03-02T10:00:00"), id="abertura-sem-fuso"),
    pytest.param(_replace("transaction_date", value="2026-10-14"), id="data-sem-hora"),
    pytest.param(_replace("transaction_date", value="2026-13-01T10:00:00-03:00"), id="data-impossivel-mes-13"),
    pytest.param(_replace("transaction_date", value="2026-02-30T10:00:00-03:00"), id="data-impossivel-30-de-fevereiro"),
    pytest.param(_replace("transaction_date", value="2026-10-17T14:15:00+16:00"), id="fuso-alem-de-14-horas"),
    pytest.param(_replace("source", "ip", value="999.1.1.1"), id="ip-com-octeto-acima-de-255"),
    pytest.param(_replace("source", "ip", value="meu-ip"), id="ip-que-nao-e-ip"),
    pytest.param(_replace("amount", value=0), id="valor-zero"),
    pytest.param(_replace("amount", value=150.5), id="valor-com-fracao-de-centavo"),
    pytest.param(_replace("amount", value="150000"), id="valor-em-texto"),
    pytest.param(_replace("source", "geolocation", value={"latitude": -23.56}), id="geolocalizacao-sem-longitude"),
    pytest.param(_replace("source", "geolocation", "latitude", value=91), id="latitude-fora-da-faixa"),
    pytest.param(_replace("source_account", "branch", value="00000000001"), id="agencia-com-11-caracteres"),
    pytest.param(_replace("destination_account", "account_number", value="1" * 21), id="conta-com-21-caracteres"),
    pytest.param(_replace("source_account", "branch", value="0001\n"), id="agencia-com-quebra-de-linha-no-fim"),
    pytest.param(_replace("id", value="pix/123"), id="id-com-barra"),
]

# O corpo que nem chega ao schema: bytes que não são JSON, ou que são mas
# não cabem. Cada caso recebe um Pix válido e devolve o que vai no fio.
RAW_BODIES = [
    pytest.param(lambda payload: b"", "application/json", id="corpo-vazio"),
    pytest.param(lambda payload: b"{", "application/json", id="json-quebrado"),
    pytest.param(lambda payload: b"[]", "application/json", id="lista-no-lugar-do-objeto"),
    pytest.param(lambda payload: b"null", "application/json", id="null"),
    pytest.param(lambda payload: b"[" * 2000 + b"]" * 2000, "application/json", id="2000-niveis-de-colchete"),
    pytest.param(lambda payload: b'{"id": "pix-\xff\xfe"}', "application/json", id="bytes-fora-de-utf-8"),
    pytest.param(
        lambda payload: json.dumps(payload).encode() + b" " * MAX_BODY_BYTES,
        "application/json",
        id="pix-valido-acima-de-16-kib",
    ),
    pytest.param(_in_pieces, "application/json", id="pix-valido-em-pedacos-sem-content-length"),
    pytest.param(lambda payload: json.dumps(payload).encode(), "text/plain", id="pix-valido-como-text-plain"),
    pytest.param(lambda payload: json.dumps(payload).encode(), None, id="pix-valido-sem-content-type"),
]

# O outro lado do schema: variações que parecem erro e são contrato.
WITHIN_CONTRACT = [
    pytest.param(_replace("transaction_date", value="2026-10-14T14:00:00.5-03:00"), id="fracao-de-1-digito"),
    pytest.param(_replace("transaction_date", value="2026-10-14T14:00:00.123456789-03:00"), id="fracao-de-9-digitos"),
    pytest.param(_replace("transaction_date", value="2026-10-14 14:00:00-03:00"), id="espaco-no-lugar-do-T"),
    pytest.param(_replace("transaction_date", value="2026-10-14T17:00:00Z"), id="utc-com-Z"),
    pytest.param(_replace("transaction_date", value="2026-10-14T17:00:00+00:00"), id="utc-com-offset-zero"),
    pytest.param(_replace("source", "geolocation", value=None), id="geolocalizacao-nula"),
    pytest.param(_replace("amount", value=5000.0), id="valor-inteiro-escrito-com-ponto-zero"),
    pytest.param(_replace("source_account", "branch", value="1234-X"), id="agencia-com-digito-x"),
]


class TestContrato:
    def test_payload_de_exemplo_do_desafio_volta_201(self):
        """O exemplo do PDF do desafio entra e sai decidido.

        22h15 em São Paulo com R$ 1.500: o L01 dispara. A conta de destino,
        aberta em 10/10 às 9h, tem 7 dias e 13 horas no momento do Pix, e
        o R01 (menos de 7 dias) fica calado. O status não é conferido
        aqui: o aparelho do exemplo volta a cada rodada da suíte, e o
        histórico dele cresce — os cenários conferem decisão; este confere
        que o contrato do desafio é aceito.
        """
        pix = post_pix(challenge_example())

        assert pix.http_status == 201
        assert pix.status in PIX_STATUSES
        assert pix.signal_status("L01") == "triggered"
        assert pix.signal_status("R01") == "not_triggered"

    def test_resposta_traz_key_status_e_metadata(self):
        """O corpo da análise tem os três campos do contrato do desafio, e o metadata, os seis dele.

        key é o uuid4 da análise; metadata leva a versão da política, a
        linha da tabela, os motivos, as duas notas, a suficiência e os 19
        sinais, na ordem do catálogo. Os números são int de verdade — um
        10.0 no lugar do 10 é o mesmo valor pro Python e outro JSON pro
        cliente.
        """
        pix = post_pix(pix_payload(new_payer(), new_recipient(), at(T0, 22), amount=150000))

        assert pix.http_status == 201
        body = pix.body
        assert set(body) == {"key", "status", "metadata"}
        assert str(UUID(body["key"])) == body["key"]
        assert UUID(body["key"]).version == 4
        assert body["status"] in PIX_STATUSES

        metadata = body["metadata"]
        assert set(metadata) == {"policy_version", "decision_rule", "reasons", "scores", "evidence_sufficiency", "signals"}
        assert metadata["policy_version"] == "v0.5.0"
        assert metadata["decision_rule"] in {
            "D-RESTRICT-SIM", "D-REJ-ATO", "D-REJ-PAY", "D-REJ-RCP",
            "D-REJ-CROSS", "D-ATO-1", "D-ATO-2", "D-PAY", "D-RCP", "D-OK",
        }
        assert isinstance(metadata["reasons"], list)
        assert metadata["reasons"]
        assert all(isinstance(reason, str) and reason for reason in metadata["reasons"])
        assert set(metadata["scores"]) == {"payer", "recipient"}
        assert all(type(score) is int for score in metadata["scores"].values())
        assert set(metadata["evidence_sufficiency"]) == {"payer", "recipient"}
        assert set(metadata["evidence_sufficiency"].values()) <= SUFFICIENCIES
        assert [signal["code"] for signal in metadata["signals"]] == list(CATALOG)
        assert all(type(signal["points"]) is int and type(signal["layer"]) is int for signal in metadata["signals"])

    def test_cada_sinal_se_descreve_pelo_catalogo(self):
        """Cada sinal diz camada, família, eixo e granularidade, e só leva ponto e evidência quando dispara.

        Fora de triggered: points 0 e nada de evidence. Um Pix às 22h de
        R$ 1.500 garante pelo menos um sinal disparado (o L01), e é ele
        que faz o lado triggered do laço rodar.
        """
        pix = post_pix(pix_payload(new_payer(), new_recipient(), at(T0, 22), amount=150000))
        assert pix.signal_status("L01") == "triggered"

        for signal in pix.metadata["signals"]:
            layer, family, axis, granularity, points = CATALOG[signal["code"]]
            described = (signal["layer"], signal["family"], signal["axis"], signal["granularity"])
            assert described == (layer, family, axis, granularity), signal
            if signal["status"] == "triggered":
                assert set(signal) == SIGNAL_KEYS | {"evidence"}, signal
                assert isinstance(signal["evidence"], dict), signal
                assert signal["evidence"], signal
                assert signal["points"] == points, signal
            else:
                assert signal["status"] in {"not_triggered", "unavailable"}, signal
                assert set(signal) == SIGNAL_KEYS, signal
                assert signal["points"] == 0, signal

    def test_get_devolve_byte_a_byte_o_corpo_do_post(self):
        """O GET devolve a análise gravada, nos mesmos bytes do POST.

        É o que o desafio pede ("devolve a análise que foi gravada"): o GET
        não roda o motor de novo, e devolve a análise como ela foi decidida.
        Repare que a comparação
        é dos bytes, e não do JSON lido: um 10.0 no lugar do 10, ou as
        chaves noutra ordem, passariam numa comparação de dicionários.
        """
        payload = pix_payload(new_payer(), new_recipient(), at(T0, 22), amount=150000)

        created = post_pix(payload)
        found = get_pix(payload["id"])

        assert created.http_status == 201
        assert found.http_status == 200
        assert found.content == created.content

    @pytest.mark.parametrize("break_payload", OUT_OF_CONTRACT)
    def test_payload_fora_do_contrato_volta_400(self, break_payload):
        """Fora do schema é 400 com o erro do Base Service (QIT000001), nunca 500."""
        payload = pix_payload(new_payer(), new_recipient(), T0)
        break_payload(payload)

        pix = post_pix(payload)

        assert pix.http_status == 400, f"aceitou {payload}"
        assert pix.body["code"] == "QIT000001"
        assert set(pix.body) == ERROR_KEYS

    def test_400_diz_o_campo_que_falhou(self):
        """A recusa diz ONDE está o problema quando o campo é aninhado.

        "'participant' was unexpected" sozinho não diz se o intruso estava
        na conta de origem ou na de destino; a descrição termina com o
        caminho.
        """
        payload = pix_payload(new_payer(), new_recipient(), T0)
        payload["source_account"]["participant"] = "60701190"

        pix = post_pix(payload)

        assert pix.http_status == 400
        assert "participant" in pix.body["description"]
        assert pix.body["description"].endswith("in source_account")

    @pytest.mark.parametrize("make_body, content_type", RAW_BODIES)
    def test_corpo_que_nao_chega_a_ser_um_pix_volta_400(self, make_body, content_type):
        """Corpo vazio, JSON quebrado, JSON que não é objeto, fundo demais, grande demais: 400, nunca 500.

        Os quatro últimos são Pix válidos no conteúdo e recusados na forma:
        acima de 16 KiB; mandado em pedaços, sem Content-Length, que não
        diz o tamanho antes de chegar; e sem Content-Type de JSON.
        """
        payload = pix_payload(new_payer(), new_recipient(), T0)

        pix = post_raw(make_body(payload), content_type=content_type)

        assert pix.http_status == 400, pix.body
        assert pix.body["code"] == "QIT000001"

    @pytest.mark.parametrize(
        "size, http_status",
        [(MAX_BODY_BYTES, 201), (MAX_BODY_BYTES + 1, 400)],
        ids=["16384-bytes-entra", "16385-bytes-volta-400"],
    )
    def test_fronteira_do_corpo_em_16_kib(self, size, http_status):
        """O corpo do POST vai até 16 KiB: 16.384 bytes entram, um byte a mais é 400.

        O Pix é válido nos dois lados e só cresce em espaços depois do
        JSON, que o JSON aceita: quem decide é o tamanho.
        """
        payload = pix_payload(new_payer(), new_recipient(), T0)
        body = json.dumps(payload).encode()

        pix = post_raw(body + b" " * (size - len(body)))

        assert pix.http_status == http_status, pix.body

    def test_pix_recusado_pelo_schema_nao_fica_gravado(self):
        """O 400 acontece antes de qualquer gravação: o id recusado não existe no GET."""
        payload = pix_payload(new_payer(), new_recipient(), T0)
        payload["extra"] = 1

        refused = post_pix(payload)
        found = get_pix(payload["id"])

        assert refused.http_status == 400
        assert found.http_status == 404

    @pytest.mark.parametrize("vary_payload", WITHIN_CONTRACT)
    def test_variacao_dentro_do_contrato_volta_201(self, vary_payload):
        """O schema cobra o fuso, e não um formato só.

        Fração de segundo de 1 a 9 dígitos, espaço no lugar do T e UTC (Z
        ou +00:00) entram. Também entram geolocation null (serializador
        costuma escrever null no opcional vazio), 5000.0 como valor
        inteiro e agência com o dígito X do Banco do Brasil.
        """
        payload = pix_payload(new_payer(), new_recipient(), T0)
        vary_payload(payload)

        pix = post_pix(payload)

        assert pix.http_status == 201, pix.body

    def test_cpf_com_digito_verificador_errado_e_aceito(self):
        """Formato certo e dígito verificador errado entra.

        O schema conta dígitos e não refaz a conta do CPF: o pagador é
        cliente nosso, o destino veio do DICT, e um CPF sintético da banca
        não pode virar 400.
        """
        payer = new_payer(document=cpf_with_wrong_check_digit())

        pix = post_pix(pix_payload(payer, new_recipient(), T0))

        assert pix.http_status == 201

    def test_documento_com_e_sem_pontuacao_e_o_mesmo_cliente(self):
        """A pontuação do documento não entra na chave do histórico.

        Preparo: 4 Pix nos 4 minutos antes de T0 com o CPF do pagador e o
        CNPJ do destino pontuados (000.000.000-00, 00.000.000/0000-00).
        Pix: o mesmo par, os dois documentos só com dígitos.

            P04 +20      5 tentativas em 5 min: os 4 pontuados contaram
            R02 +16      5 entradas em 10 min no mesmo titular
            REL01 calado o par já tem Pix aprovado

        Se a pontuação entrasse na chave, este Pix seria de outro cliente,
        no primeiro dia dele — P04 unavailable.
        """
        payer, company = new_payer(), new_recipient(company=True)
        formatted = {
            "payer_document": format_document(payer.document),
            "recipient_document": format_document(company.document),
        }
        seed([pix_payload(payer, company, moment, **formatted) for moment in minutes_before(T0, 4)])

        pix = post_pix(pix_payload(payer, company, T0))

        assert pix.http_status == 201
        assert pix.signal_status("P04") == "triggered"
        assert pix.signal("P04")["evidence"]["attempts_5m"] == 5
        assert pix.signal_status("R02") == "triggered"
        assert pix.signal_status("REL01") == "not_triggered"

    @pytest.mark.parametrize("token", [None, "token-errado"], ids=["sem-token", "token-errado"])
    def test_post_sem_token_valido_volta_403(self, token):
        """Sem o INTERNAL-TOKEN certo, o Pix nem chega ao motor — e não fica gravado."""
        payload = pix_payload(new_payer(), new_recipient(), T0)

        refused = post_pix(payload, token=token)

        assert refused.http_status == 403
        assert refused.body["code"] == "QIT000002"
        assert get_pix(payload["id"]).http_status == 404

    @pytest.mark.parametrize("token", [None, "token-errado"], ids=["sem-token", "token-errado"])
    def test_get_sem_token_valido_volta_403(self, token):
        """A análise gravada também é interna: sem token, 403, mesmo para um id que existe."""
        payload = pix_payload(new_payer(), new_recipient(), T0)
        assert post_pix(payload).http_status == 201

        refused = get_pix(payload["id"], token=token)

        assert refused.http_status == 403
        assert refused.body["code"] == "QIT000002"

    def test_get_de_pix_que_nunca_chegou_volta_404(self):
        pix = get_pix(f"pix-{uuid4().hex}")

        assert pix.http_status == 404
        assert pix.body["code"] == "QIT001001"
        assert set(pix.body) == ERROR_KEYS

    def test_get_com_byte_nulo_no_id_volta_404(self):
        """Um id que nenhum POST aceitaria (%00 no caminho) é 404, e não 500."""
        pix = get_pix("\x00")

        assert pix.http_status == 404

    @pytest.mark.parametrize("method", ["GET", "POST"])
    def test_rota_com_barra_no_fim_volta_404(self, method):
        """/pix/transaction/, com a barra e sem o id, é rota que não existe: 404, sem redirecionar."""
        response = ClientRequisition.send(method, "/pix/transaction/", headers={"INTERNAL-TOKEN": INTERNAL_TOKEN})

        assert response.response_status == 404
        assert response.response_json["code"] == "QIT000404"

    def test_reenvio_devolve_mesma_analise(self):
        """Cenário 15 — o mesmo Pix duas vezes: 201, depois 200 com a MESMA análise, nos mesmos bytes.

        E o reenvio não conta de novo no histórico. A prova é o P04, que
        conta tentativas do pagador em 5 min, e o R02, que conta entradas
        no destino em 10 min — os dois disparam a partir de 5:

            T0-4min, T0-3min   duas sementes, S1 e S2
            T0-2min            o Pix X, mandado DUAS vezes (201 e 200)
            T0-1min            Y vê S1, S2 e X: 3 + 1 = 4 → P04 e R02 calados
            T0                 Z vê S1, S2, X e Y: 4 + 1 = 5 → os dois disparam

        Se o reenvio contasse, Y já veria 5 e dispararia. Z disparando
        mostra que a conta é exata, e não que o P04 está quebrado.
        """
        payer, recipient = new_payer(), new_recipient()
        seed_pix(payer, recipient, [T0 - timedelta(minutes=4), T0 - timedelta(minutes=3)])
        resent = pix_payload(payer, recipient, T0 - timedelta(minutes=2))

        first = post_pix(resent)
        again = post_pix(resent)

        assert first.http_status == 201
        assert again.http_status == 200
        assert again.content == first.content
        assert get_pix(resent["id"]).content == first.content

        after = post_pix(pix_payload(payer, recipient, T0 - timedelta(minutes=1)))
        assert after.http_status == 201
        assert after.signal_status("P04") == "not_triggered"
        assert after.signal_status("R02") == "not_triggered"

        last = post_pix(pix_payload(payer, recipient, T0))
        assert last.http_status == 201
        assert last.signal_status("P04") == "triggered"
        assert last.signal("P04")["evidence"]["attempts_5m"] == 5
        assert last.signal_status("R02") == "triggered"

    def test_reenvio_com_as_chaves_em_outra_ordem_volta_200(self):
        """Ordem de chave não é conteúdo.

        O reenvio compara o JSON como dicionário, e o mesmo Pix escrito
        noutra ordem é o mesmo Pix.
        """
        payload = pix_payload(new_payer(), new_recipient(), T0)
        reordered = _reversed_keys(payload)
        assert json.dumps(reordered) != json.dumps(payload)

        first = post_pix(payload)
        again = post_pix(reordered)

        assert first.http_status == 201
        assert again.http_status == 200
        assert again.content == first.content

    def test_reenvio_com_o_documento_noutra_pontuacao_volta_409(self):
        """A idempotência compara o payload como chegou, e não o normalizado.

        O mesmo Pix com o CPF agora pontuado é, para o motor, o mesmo
        pagador — mas não é o mesmo payload, e volta 409. A escolha é
        deliberada: o que fica gravado é o que chegou, e um reenvio que
        mudou algum valor não é reenvio.
        """
        payer = new_payer()
        payload = pix_payload(payer, new_recipient(), T0)
        formatted = copy.deepcopy(payload)
        formatted["source_account"]["owner"]["document_number"] = format_document(payer.document)

        first = post_pix(payload)
        conflict = post_pix(formatted)

        assert first.http_status == 201
        assert conflict.http_status == 409
        assert conflict.body["code"] == "QIT001002"

    def test_mesmo_id_payload_diferente_409(self):
        """Cenário 15 — o mesmo id com outro conteúdo: 409, e a análise gravada não muda."""
        payload = pix_payload(new_payer(), new_recipient(), T0)
        changed = copy.deepcopy(payload)
        changed["amount"] += 100

        first = post_pix(payload)
        conflict = post_pix(changed)

        assert first.http_status == 201
        assert conflict.http_status == 409
        assert conflict.body["code"] == "QIT001002"
        assert set(conflict.body) == ERROR_KEYS
        assert get_pix(payload["id"]).content == first.content
