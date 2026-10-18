from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """A raiz de todos os models deste pacote.

    Quem cria as tabelas é o database/database.sql, quando o Postgres
    nasce. O SQLAlchemy só descreve o que já está lá — ninguém chama
    `Base.metadata.create_all`. Mesmo assim cada model repete os UNIQUE,
    os CHECK e os índices do SQL: quem lê o model enxerga as constraints
    sem abrir outro arquivo, e um `create_all` numa base vazia dá o mesmo
    schema. Se os dois discordarem, quem manda é o SQL, e o model está
    errado.
    """
