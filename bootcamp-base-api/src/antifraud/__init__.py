"""O motor antifraude Pix: recebe um Context pronto e devolve a Analysis.

Puro de propósito: não importa banco, FastAPI nem nada de fora deste
pacote. Quem busca o histórico e consulta o DICT é o controller; o motor
só faz a conta. O controller chama assim:

    from antifraud import evaluate
    analysis = evaluate(context)

O Context e as peças dele estão em contract.py; os números, em policy.py.
"""

from antifraud.contract import (
    LOCAL_TIMEZONE,
    Analysis,
    Axis,
    Context,
    DeviceHistory,
    Evidence,
    Layer,
    PairHistory,
    PayerHistory,
    PixStatus,
    RecipientHistory,
    Signal,
    SignalStatus,
    Sufficiency,
    Transaction,
)
from antifraud.engine import LAYERS, evaluate
from antifraud.policy import POLICY_VERSION
