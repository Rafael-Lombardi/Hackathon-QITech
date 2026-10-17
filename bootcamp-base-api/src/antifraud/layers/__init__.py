"""As três camadas do motor, uma por arquivo, todas no formato do Layer
de contract.py: recebem o Context e devolvem os sinais delas.

Nenhuma camada importa outra; o porquê está no próprio Layer. Quem põe
as três em fila é o LAYERS, no engine.
"""

from antifraud.layers.decision_tree import DecisionTreeLayer
from antifraud.layers.initial_checks import InitialChecksLayer
from antifraud.layers.trust_factor import TrustFactorLayer
