"""As camadas do motor, uma por arquivo, todas no formato do Layer
de contract.py: recebem o Context e devolvem os sinais delas.

Nenhuma camada importa outra; o porquê está no próprio Layer. Quem põe
as quatro em fila é o LAYERS, no engine — a camada 3, a do DICT, são
duas classes: a estatística de segurança e a marcação de fraude.
"""

from antifraud.layers.decision_tree import DecisionTreeLayer
from antifraud.layers.dict_security import DictSecurityLayer
from antifraud.layers.dict_security_statistics import DictSecurityStatisticsLayer
from antifraud.layers.initial_checks import InitialChecksLayer
