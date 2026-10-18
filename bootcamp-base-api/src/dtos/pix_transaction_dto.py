from antifraud import signal_spec
from models import PixTransaction, PixTransactionRuleResult


class PixTransactionDTO:
    """Traduz o Pix gravado no JSON que a API devolve.

    O repository entrega um PixTransaction e as linhas de
    pix_transaction_rule_result — o espelho das tabelas, com nome de
    coluna e chave estrangeira. Aqui elas viram o corpo do contrato do
    desafio, {key, status, metadata}, com os nomes da RFC: a
    pix_transaction_key vira key, payer_risk e recipient_risk viram
    scores, rule_name vira code e rule_result_data vira evidence.

    O POST e o GET passam pelo mesmo método, com as mesmas linhas: o POST
    lê de volta do banco os sinais que acabou de gravar, em vez de usar a
    Analysis que o motor devolveu na memória. Custa duas leituras por
    índice a cada POST, os sinais e o status. Em troca, o corpo do POST e
    o do GET são o mesmo por construção — não existe um segundo caminho
    que possa divergir do primeiro.

    Campo novo na resposta se acrescenta aqui — e só aqui.
    """

    @staticmethod
    def obj_to_dict(pix_transaction: PixTransaction, rule_results: list[PixTransactionRuleResult]) -> dict:
        """A análise gravada, com os sinais na ordem em que foram gravados.

        Essa ordem é a do catálogo do motor: o motor ordena os sinais, o
        controller grava nessa ordem num INSERT só, e o list_signals lê de
        volta pela ordem do id.

        family e axis vêm da linha, como a regra disse na hora. Camada e
        granularidade não são coluna: saem do catálogo pelo código, no
        signal_spec. Repare no preço. Se um dia um código sair do
        catálogo, o GET dos Pix antigos dele quebra aqui — e é a hora de
        gravar as duas, ou de guardar o catálogo de cada policy_version.
        """
        signals = []

        for rule_result in rule_results:
            spec = signal_spec(rule_result.rule_name)

            signal_dto = {
                "code": rule_result.rule_name,
                "layer": spec.layer,
                "granularity": spec.granularity,
                "family": rule_result.family,
                "axis": rule_result.axis,
                "status": rule_result.signal_status.enumerator,
                "points": rule_result.points,
            }

            # Evidência só quando há o que mostrar, como no exemplo da RFC:
            # not_triggered e unavailable gravam {} e saem sem a chave — o
            # status já diz por quê.
            if rule_result.rule_result_data:
                signal_dto["evidence"] = rule_result.rule_result_data

            signals.append(signal_dto)

        return {
            "key": pix_transaction.pix_transaction_key,
            "status": pix_transaction.status.enumerator,
            "metadata": {
                "policy_version": pix_transaction.policy_version,
                "decision_rule": pix_transaction.decision_rule,
                "reasons": pix_transaction.reasons,
                "scores": {
                    "payer": pix_transaction.payer_risk,
                    "recipient": pix_transaction.recipient_risk,
                },
                "evidence_sufficiency": {
                    "payer": pix_transaction.payer_sufficiency,
                    "recipient": pix_transaction.recipient_sufficiency,
                },
                "signals": signals,
            },
        }
