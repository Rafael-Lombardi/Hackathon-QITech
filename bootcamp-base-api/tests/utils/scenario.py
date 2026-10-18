"""Modelo declarativo para cenários de risco reproduzíveis.

Um cenário é deliberadamente dividido em cinco partes:

1. ``authoritative``: dados que futuramente virão de seeds de cadastro
   (dispositivo, reputação, restrições, DICT);
2. ``history``: Pix observados, sempre enviados pela API;
3. ``derived``: baseline e demais agregados, que o sistema deve calcular;
4. ``request``: o Pix sob avaliação;
5. ``expected``: oráculo independente do cenário.

``authoritative`` é seedado separadamente (device registry, restrições e
inteligência externa); ``derived`` continua sendo o oráculo documental que
o sistema deve calcular. Esses dados não são gravados como transações:
``history`` passa pela mesma validação e decisão que uma requisição normal.
O resultado esperado também não é inserido no banco; ele é comparado com o
POST e com o GET, provando que a análise foi persistida.
"""

from dataclasses import dataclass, field
from typing import Any

import pytest

from tests.utils.pix import PixResponse, get_pix, post_pix, seed
from tests.utils.device_registry import seed_devices
from tests.utils.risk_sources import seed_dict_security_evidence, seed_restrictions


@dataclass(frozen=True)
class ExpectedAnalysis:
    """Oráculo mínimo, mas completo para a decisão e os sinais."""

    status: str
    decision_rule: str
    scores: dict[str, int]
    sufficiency: dict[str, str]
    signals: dict[str, str]
    points: dict[str, int] = field(default_factory=dict)

    def assert_matches(self, response: PixResponse) -> None:
        assert response.status == self.status
        assert response.decision_rule == self.decision_rule
        assert response.scores == self.scores
        assert response.sufficiency == self.sufficiency

        actual_signals = {
            signal["code"]: signal for signal in response.metadata["signals"]
        }
        assert set(actual_signals) == set(self.signals)
        assert {
            code: signal["status"] for code, signal in actual_signals.items()
        } == self.signals
        for code, expected_points in self.points.items():
            assert actual_signals[code]["points"] == expected_points


@dataclass(frozen=True)
class Scenario:
    """Caso executável pela porta pública da aplicação."""

    name: str
    request: dict[str, Any]
    expected: ExpectedAnalysis
    history: tuple[dict[str, Any], ...] = ()
    history_status: tuple[str, ...] = ()
    authoritative: dict[str, Any] = field(default_factory=dict)
    derived: dict[str, Any] = field(default_factory=dict)

    def run(self) -> PixResponse:
        """Monta o histórico, avalia o caso e confere a análise persistida."""
        statuses = self.history_status or ("approved",) * len(self.history)
        if len(statuses) != len(self.history):
            raise AssertionError(f"{self.name}: um status esperado por semente")

        # Sementes entram pela API: nunca inserimos uma linha de histórico
        # diretamente, para que o caso teste o mesmo caminho de produção.
        seed_devices(self.authoritative.get("devices", []))
        seed_restrictions(self.authoritative.get("restrictions", []))
        seed_dict_security_evidence(self.authoritative.get("dict_security", self.authoritative.get("external_risk", [])))
        seed(list(self.history), list(statuses), register_devices=False)
        response = post_pix(self.request, auto_register=False)
        if response.http_status != 201:
            pytest.fail(f"{self.name}: avaliação retornou HTTP {response.http_status}: {response.body!r}")
        self.expected.assert_matches(response)

        # GET é a prova de que o resultado e os sinais foram colocados no DB,
        # em vez de apenas devolvidos a partir do objeto em memória.
        persisted = get_pix(self.request["id"])
        assert persisted.http_status == 200
        assert persisted.body == response.body
        return response
