from uuid import uuid4

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert

from database import Context
from models import Customer


class CustomerRepository:
    """O customer de cada documento, e a trava do pagador.

    Não dá commit — quem diz que o trabalho terminou é o controller — e
    não decide regra nenhuma.

    Repare na ordem quando um Pix traz dois documentos: os upserts vêm em
    ordem de documento, e não pagador primeiro. Dois Pix cruzados entre
    documentos inéditos (A paga B enquanto B paga A) criariam os dois
    customers em ordens opostas, cada um esperando o INSERT do outro — o
    Postgres derruba um deles com DeadlockDetected, reproduzido. Quem
    ordena é o controller.
    """

    def __init__(self, context: Context) -> None:
        self.session = context.db_session

    def get_or_create(self, document_number: str) -> Customer:
        """Devolve o customer do documento — e cria, se for a primeira vez.

        O caminho comum é o primeiro SELECT: documento que já pagou ou
        recebeu volta direto, sem escrita nenhuma. Se o SELECT não acha,
        vem o upsert do database.sql, INSERT ... ON CONFLICT DO NOTHING
        RETURNING, que volta vazio num caso só: outra requisição inseriu o
        mesmo documento entre o nosso SELECT e o nosso INSERT. O Postgres
        segura o nosso INSERT até a outra transação terminar e, se ela
        commitou, desiste em silêncio. O SELECT do fim acha a linha dela —
        em READ COMMITTED cada comando enxerga o que já foi commitado.

        Duas formas mais curtas perderam. ON CONFLICT DO NOTHING sempre,
        sem o SELECT antes, custa 0,5 ms em vez de 0,2 e queima um id da
        sequência a cada Pix. DO UPDATE ... RETURNING, que devolve a linha
        nos dois casos, reescreve uma linha que não mudou: lock de UPDATE
        e uma versão morta da linha a cada chamada.
        """
        by_document = select(Customer).where(Customer.document_number == document_number)

        customer = self.session.scalars(by_document).one_or_none()
        if customer is not None:
            return customer

        created = self.session.scalars(
            insert(Customer)
            .values(customer_key=str(uuid4()), document_number=document_number)
            .on_conflict_do_nothing(index_elements=[Customer.document_number])
            .returning(Customer)
        ).one_or_none()
        if created is not None:
            return created

        return self.session.scalars(by_document).one()

    def lock_for_pix_document(self, *, document_number: str) -> None:
        """Trava o pagador até o fim da transação: um advisory lock pelo documento.

        Dois Pix do mesmo pagador chegando juntos passam um de cada vez,
        mesmo saindo de contas diferentes: o segundo espera aqui o commit
        (ou o rollback) do primeiro e, quando segue, já enxerga o Pix que o
        primeiro gravou. É por isso que o controller chama isto ANTES de
        contar o histórico — e antes dos upserts.

        Pelo documento, e não pela linha do customer, porque a linha só
        existe depois do get_or_create: dois primeiros Pix do mesmo
        documento atravessariam essa janela juntos, cada um contando um
        histórico vazio. E pelo pagador só: travar também o recebedor
        poria todos os pagadores de uma loja em fila única.

        O hashtextextended reduz o documento a 64 bits; dois documentos no
        mesmo hash só esperariam um pelo outro à toa. O lock é transacional
        (xact): some sozinho no commit ou no rollback, sem deixar estado no
        banco.

        Só funciona em READ COMMITTED, o padrão. Em REPEATABLE READ o
        segundo espera e mesmo assim lê o histórico de antes da espera.
        """
        self.session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:document, 0))"),
            {"document": document_number},
        )
