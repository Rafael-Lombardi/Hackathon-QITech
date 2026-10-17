CREATE TABLE sample_entity_status(
    id		                        SERIAL PRIMARY KEY,
    enumerator                      VARCHAR(50) NOT NULL,
    created_at                      TIMESTAMP NOT NULL DEFAULT(NOW()),
    UNIQUE(enumerator)
);

INSERT INTO sample_entity_status (enumerator) VALUES
('created'),
('pending'),
('success'),
('failed');

CREATE TABLE sample_entity(
    id                              SERIAL PRIMARY KEY,
    sample_entity_key               CHAR(36) NOT NULL,
    status_id                       INTEGER NOT NULL REFERENCES sample_entity_status(id),
    sample_entity_data              JSONB NOT NULL,
    name                            VARCHAR(255) NOT NULL,
    email                           VARCHAR(255) NOT NULL,
    document_number                 CHAR(14) NOT NULL,
    birthdate                       DATE NOT NULL,
    counter                         INTEGER NOT NULL,
    created_at                      TIMESTAMP NOT NULL DEFAULT(NOW()),
    UNIQUE(sample_entity_key),
    UNIQUE(document_number),
    UNIQUE(email)
);

CREATE TABLE sample_entity_status_event(
    id                              SERIAL PRIMARY KEY,
    sample_entity_id                INTEGER NOT NULL REFERENCES sample_entity(id),
    status_id                       INTEGER NOT NULL REFERENCES sample_entity_status(id),
    event_datetime                  TIMESTAMP NOT NULL,
    created_at                      TIMESTAMP NOT NULL DEFAULT(NOW())
);

-- Daqui para baixo é o antifraude Pix. As tabelas sample_entity* acima
-- ficam até a entidade de exemplo ser trocada: o código e os testes dela
-- ainda dependem delas.
--
-- O modelo, em três tabelas:
--   pix_transaction_status       os três veredictos do contrato (enum).
--   pix_transaction              uma linha por Pix: payload + decisão.
--   pix_transaction_rule_result  uma linha por regra por Pix: o metadata.
-- Os índices são um por eixo de histórico (pagador, aparelho, IP,
-- destino), todos terminando em transaction_date.

-- As três decisões do contrato, e só elas. O Pix já nasce decidido (o
-- controller roda as regras antes do INSERT), então não existe pendente
-- aqui: o que chega sem decisão é erro, não estado.
CREATE TABLE pix_transaction_status(
    id                              SERIAL PRIMARY KEY,
    enumerator                      VARCHAR(50) NOT NULL,
    created_at                      TIMESTAMP NOT NULL DEFAULT(NOW()),
    UNIQUE(enumerator)
);

INSERT INTO pix_transaction_status (enumerator) VALUES
('approved'),
('rejected'),
('two_factor_required');

-- Uma linha por Pix recebido: o que chegou E o que decidimos sobre ele.
--
-- Transação e análise moram na mesma tabela de propósito. São 1:1 — o
-- mesmo Pix nunca gera duas análises — e toda regra que olha o histórico
-- olha também a decisão passada ("quantos Pix aprovados este pagador fez
-- hoje?"). Tabela separada custaria um JOIN em cada consulta dessas. Por
-- isso a pix_transaction_key, a key da resposta, identifica os dois.
--
-- Não há tabela account. Ninguém cadastra conta neste serviço, a de
-- destino é de outro banco e cada payload traz as duas inteiras de novo.
-- Só os documentos viram coluna, porque são o eixo das regras; agência,
-- conta e ISPB ficam no payload bruto até alguma regra precisar deles —
-- e aí viram coluna junto com o índice que a regra pedir.
--
-- O que se perde é uma linha de pagador pra travar com SELECT ... FOR
-- UPDATE. Dois Pix do mesmo pagador chegando juntos passam um de cada
-- vez por um pg_advisory_xact_lock no document_number de origem, lá no
-- controller, antes da primeira consulta de regra e na mesma transação do
-- INSERT. Isso só fecha a corrida das regras por pagador (aparelho, IP e
-- destino continuam com ela, e tudo bem: são limiares, não saldo) e só
-- funciona em READ COMMITTED, que é o padrão.
--
-- O payload vai inteiro em pix_transaction_data, do jeito que chegou. As
-- colunas são o que o motor leu dele; se um dia uma leitura estiver
-- errada, dá pra refazer todas a partir do JSONB. É a maior coluna da
-- tabela (uns 650 bytes por Pix), e a primeira alavanca de retenção
-- quando o disco apertar.
--
-- Sobre as colunas, o que não está escrito no tipo:
--
-- • external_id é o id do payload (pix-000123) e o {id} do GET. O UNIQUE
--   nele é a idempotência: duas tentativas do mesmo Pix chegando juntas,
--   só uma grava — o if do controller resolve a repetição comum, o banco
--   resolve a corrida.
--
-- • amount em centavos, inteiro, BIGINT: R$ 21 milhões já estouram um
--   INTEGER, e dinheiro nunca é float. O id também é BIGSERIAL, diferente
--   do Base Service: a 20 milhões de Pix por mês, INT4 dura nove anos
--   aqui e menos de dois na tabela de regras logo abaixo, e trocar depois
--   reescreve a tabela inteira com ela travada.
--
-- • transaction_date e as opening_date são TIMESTAMPTZ, diferente do
--   TIMESTAMP que o Base Service usa em created_at. created_at é o
--   relógio do nosso servidor; essas três vêm de fora, com fuso
--   ("2026-10-17T22:15:00-03:00"), e TIMESTAMP descartaria o -03:00 em
--   silêncio: as 22h15 de São Paulo virariam 22h15 de lugar nenhum e a
--   regra de horário noturno (20h às 6h) erraria em três horas.
--
--   Repare que TIMESTAMPTZ guarda o instante, não o fuso: o banco roda em
--   UTC, e EXTRACT(HOUR FROM transaction_date) devolve 1, não 22, para o
--   Pix do exemplo. Toda regra que olha hora ou dia converte antes —
--   transaction_date AT TIME ZONE 'America/Sao_Paulo' no SQL,
--   .astimezone() no Python. O -03:00 literal está no payload bruto.
--
-- • document_number é VARCHAR(18), não CHAR(14): o contrato aceita CNPJ,
--   e "12.345.678/0001-90" tem 18. Fica como chega, mascarado; o JSON
--   Schema só aceita as duas máscaras, senão o índice deixa de agrupar o
--   mesmo pagador.
--
-- • external_id e device_id são opacos ("ID único", "ID do aparelho"), por
--   isso 255. ip em VARCHAR(45) cabe IPv6; INET daria operadores de rede
--   que nenhuma regra usa. latitude/longitude vêm as duas ou nenhuma, e
--   dentro da faixa: fora dela o erro seria do tipo, e viraria 500.
CREATE TABLE pix_transaction(
    id                              BIGSERIAL PRIMARY KEY,
    pix_transaction_key             CHAR(36) NOT NULL,
    status_id                       INTEGER NOT NULL REFERENCES pix_transaction_status(id),
    pix_transaction_data            JSONB NOT NULL,
    external_id                     VARCHAR(255) NOT NULL,
    amount                          BIGINT NOT NULL,
    transaction_date                TIMESTAMPTZ NOT NULL,
    source_document_number          VARCHAR(18) NOT NULL,
    source_opening_date             TIMESTAMPTZ NOT NULL,
    destination_document_number     VARCHAR(18) NOT NULL,
    destination_opening_date        TIMESTAMPTZ NOT NULL,
    device_id                       VARCHAR(255) NOT NULL,
    channel                         VARCHAR(30) NOT NULL,
    ip                              VARCHAR(45) NOT NULL,
    latitude                        NUMERIC(9, 6),
    longitude                       NUMERIC(9, 6),
    created_at                      TIMESTAMP NOT NULL DEFAULT(NOW()),
    UNIQUE(pix_transaction_key),
    UNIQUE(external_id),
    CHECK(jsonb_typeof(pix_transaction_data) = 'object'),
    CHECK(amount > 0),
    CHECK((latitude IS NULL) = (longitude IS NULL)),
    CHECK(latitude IS NULL OR latitude BETWEEN -90 AND 90),
    CHECK(longitude IS NULL OR longitude BETWEEN -180 AND 180)
);

-- Cada índice abaixo responde a uma pergunta de regra, e todos terminam
-- em transaction_date porque a pergunta é sempre "deste X, numa janela
-- de tempo". A janela conta em transaction_date, não em created_at:
-- histórico se conta no tempo do negócio, é o que deixa o teste mandar a
-- data no payload, e um Pix que chegar atrasado continua no lugar certo.
--
-- Pagador: quantos Pix e quanto dinheiro em 1 min, 10 min, 1 h, 24 h;
-- valor e horário habituais; aparelho ou destinatário que este pagador
-- nunca usou. Esta última não precisa de índice de par enquanto o
-- histórico de um pagador for pequeno; quando a regra existir e doer, o
-- índice (source_document_number, destination_document_number) entra
-- com CREATE INDEX CONCURRENTLY, sem mexer em mais nada.
CREATE INDEX pix_transaction_source_document_number_idx
    ON pix_transaction (source_document_number, transaction_date);

-- Aparelho: quantos Pix por device_id e aparelho compartilhado (um
-- aparelho pagando por vários CPFs distintos é indício de farm de contas).
CREATE INDEX pix_transaction_device_id_idx
    ON pix_transaction (device_id, transaction_date);

-- IP: a mesma pergunta do aparelho, para o endereço.
CREATE INDEX pix_transaction_ip_idx
    ON pix_transaction (ip, transaction_date);

-- Destino: muitos pagadores distintos para o mesmo recebedor em janela
-- curta (robô multiplicador, loja falsa, conta laranja). Indexado pelo
-- documento, não por ISPB+agência+conta: o laranja com três contas em
-- três bancos continua sendo um documento só.
CREATE INDEX pix_transaction_destination_document_number_idx
    ON pix_transaction (destination_document_number, transaction_date);

-- A explicação da decisão: uma linha por regra avaliada, por Pix. É
-- daqui que sai o metadata da resposta e é isto que o README promete:
-- quais regras rodaram, o que cada uma viu e o que cada uma pediu.
--
-- status_id é o que a regra, sozinha, diria; a decisão final é a mais
-- severa entre elas. rule_result_data guarda os números que a regra viu
-- (observado, limite, janela), porque os parâmetros vão ser calibrados e
-- o motivo de ontem tem de continuar legível com os parâmetros de amanhã.
--
-- O nome da regra é texto, não FK pra uma tabela de regras: regra é
-- código, nasce e muda no motor, e uma tabela populada por INSERT aqui
-- custaria um docker compose down -v a cada regra nova. Os três status
-- são fixos pelo contrato; as regras não.
--
-- Append-only: ninguém dá UPDATE aqui. O UNIQUE diz que cada regra roda
-- uma vez por análise e, de quebra, é o índice que o GET usa pra montar
-- a resposta.
CREATE TABLE pix_transaction_rule_result(
    id                              BIGSERIAL PRIMARY KEY,
    pix_transaction_id              BIGINT NOT NULL REFERENCES pix_transaction(id),
    status_id                       INTEGER NOT NULL REFERENCES pix_transaction_status(id),
    rule_result_data                JSONB NOT NULL,
    rule_name                       VARCHAR(50) NOT NULL,
    created_at                      TIMESTAMP NOT NULL DEFAULT(NOW()),
    UNIQUE(pix_transaction_id, rule_name),
    CHECK(rule_name <> ''),
    CHECK(jsonb_typeof(rule_result_data) = 'object')
);

-- Não há pix_transaction_status_event: a decisão nasce pronta e o
-- contrato não tem rota que a mude. No dia em que o 2FA for confirmado
-- pela API, essa tabela nasce junto com o teste dela.
