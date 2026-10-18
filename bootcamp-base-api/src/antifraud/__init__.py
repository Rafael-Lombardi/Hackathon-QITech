"""O motor antifraude Pix: recebe um Context pronto e devolve a Analysis.

No Base Service a regra de negócio mora no controller. Aqui são 20
sinais, tetos e uma tabela de decisão, que no controller ficariam
misturados com trava, sessão e commit: o motor é a parte da regra que
não precisa de banco. Não importa SQLAlchemy, FastAPI nem nada de fora
deste pacote. Quem busca o histórico e consulta o DICT continua sendo o
controller, que chama assim:

    from antifraud import evaluate
    analysis = evaluate(context)

O Context e as peças dele estão em contract.py; os números, em
policy.py.
"""

from antifraud.contract import (
    LOCAL_TIMEZONE,
    Analysis,
    Axis,
    Context,
    DeviceHistory,
    PairHistory,
    PayerHistory,
    PixStatus,
    RecipientHistory,
    Signal,
    SignalStatus,
    Sufficiency,
    Transaction,
)
from antifraud.engine import evaluate
from antifraud.policy import POLICY_VERSION, SignalSpec, signal_spec
