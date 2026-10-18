from models import PixAnalysis, PixTransaction


class PixTransactionDTO:
    """Expõe o fato recebido e a decisão 1:1 persistida."""

    @staticmethod
    def obj_to_dict(pix_transaction: PixTransaction, analysis: PixAnalysis) -> dict:
        stored = analysis.analysis_metadata or {}
        # Keep the public contract explicit. Internal snapshots stay in the
        # database but are not leaked as extra response fields.
        metadata = {
            "policy_version": analysis.policy_version,
            "decision_rule": analysis.decision_rule,
            "reasons": analysis.reasons,
            "scores": {
                "payer": analysis.payer_risk,
                "recipient": analysis.recipient_risk,
            },
            "evidence_sufficiency": {
                "payer": analysis.payer_sufficiency,
                "recipient": analysis.recipient_sufficiency,
            },
            "family_contributions": stored.get("family_contributions", {}),
            "signals": stored.get("signals", []),
        }
        return {
            "key": pix_transaction.pix_transaction_key,
            "status": analysis.status.enumerator,
            "metadata": metadata,
        }
