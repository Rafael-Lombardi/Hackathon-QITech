"""Pix use-case boundary; risk rules and persistence join later."""

from dtos.pix_transaction_dto import PixDecision, PixTransactionInput


class PixTransactionController:
    def decide(self, transaction: PixTransactionInput) -> PixDecision:
        raise NotImplementedError("Pix risk rules are not implemented")
