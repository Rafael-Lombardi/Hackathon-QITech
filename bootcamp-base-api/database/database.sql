CREATE TABLE pix_transaction_status(
    id SERIAL PRIMARY KEY,
    enumerator VARCHAR(50) NOT NULL UNIQUE,
    created_at TIMESTAMP NOT NULL DEFAULT NOW()
);

INSERT INTO pix_transaction_status (enumerator) VALUES
('approved'), ('rejected'), ('two_factor_required');

-- Quem paga ou recebe, um por documento. Separado de account porque um
-- documento tem contas em bancos diferentes: o laranja com três contas em
-- três bancos é um documento só, e as regras por pessoa agrupam por aqui.
--
-- Ninguém cadastra customer nem account neste serviço. Os dois nascem na
-- primeira vez que aparecem num payload, por INSERT ... ON CONFLICT DO
-- NOTHING no controller — é isso que o resto deste arquivo chama de
-- upsert, e são até quatro por Pix: customer e account, origem e destino
-- — e a linha não muda depois. Conta que reaparece com outro documento é
-- sinal pra regra, não erro de banco: a linha original fica, o payload
-- bruto guarda o que chegou e o controller decide. Por isso só o
-- REFERENCES customer(id) em account amarra conta a documento.
--
-- document_number é VARCHAR(18), não CHAR(14): o contrato aceita CNPJ, e
-- "12.345.678/0001-90" tem 18. Fica como chega, mascarado; o JSON Schema
-- só aceita as duas máscaras, senão o UNIQUE deixa de agrupar o mesmo
-- documento.
CREATE TABLE customer(
    id                              BIGSERIAL PRIMARY KEY,
    customer_key                    CHAR(36) NOT NULL,
    document_number                 VARCHAR(18) NOT NULL,
    created_at                      TIMESTAMP NOT NULL DEFAULT(NOW()),
    UNIQUE(customer_key),
    UNIQUE(document_number)
);

-- Uma conta, de qualquer banco, de um customer. É a linha que o
-- controller trava — SELECT ... FOR NO KEY UPDATE na conta de origem,
-- depois dos upserts e na mesma transação do INSERT do Pix — como a
-- estante da RFC da biblioteca: ninguém conta os livros sem segurar a
-- estante. Dois Pix da mesma conta de origem chegando juntos passam um de
-- cada vez. Repare no NO KEY: o INSERT do Pix confere as duas FKs com um
-- FOR KEY SHARE nas contas, e um FOR UPDATE comum brigaria com ele —
-- dois Pix cruzados (1 paga 4, 4 paga 1) dariam deadlock. O resto da
-- história mora no controller.
--
-- participant é o ISPB, o código de oito dígitos de cada banco no Pix.
-- Fica NULL na conta de origem: o payload não o traz e o contrato não diz
-- qual é o nosso. Daí o NULLS NOT DISTINCT (Postgres 15 em diante; o
-- Dockerfile pina a 16): sem ele NULL nunca é igual a NULL, cada Pix
-- criaria a conta de origem de novo e o ON CONFLICT ficaria surdo. Quando
-- o nosso ISPB for conhecido: um UPDATE e um NOT NULL.
CREATE TABLE account(
    id                              BIGSERIAL PRIMARY KEY,
    account_key                     CHAR(36) NOT NULL,
    customer_id                     BIGINT NOT NULL REFERENCES customer(id),
    participant                     CHAR(8),
    branch                          VARCHAR(10) NOT NULL,
    account_number                  VARCHAR(20) NOT NULL,
    opening_date                    TIMESTAMPTZ NOT NULL,
    created_at                      TIMESTAMP NOT NULL DEFAULT(NOW()),
    UNIQUE(account_key),
    UNIQUE NULLS NOT DISTINCT (participant, branch, account_number),
    CHECK(participant IS NULL OR participant ~ '^[0-9]{8}$')
);

-- Das contas ao documento: "muitos pagadores pro mesmo documento de
-- destino" e "quantas contas este documento já usou" são JOIN
-- account → customer, e passam por aqui. Não há índice em participant
-- nem em (branch, account_number) além do UNIQUE: o upsert procura pela
-- tripla inteira, e o UNIQUE já é esse índice.
CREATE INDEX account_customer_id_idx
    ON account (customer_id);

-- Uma linha por Pix recebido: o que chegou E o que decidimos sobre ele.
--
-- Transação e análise moram na mesma tabela de propósito. São 1:1 — o
-- mesmo Pix nunca gera duas análises — e toda regra que olha o histórico
-- olha também a decisão passada ("quantos Pix aprovados este pagador fez
-- hoje?"). Tabela separada custaria um JOIN em cada consulta dessas. Por
-- isso a pix_transaction_key, a key da resposta, identifica os dois.
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
--   INTEGER, e dinheiro nunca é float. Os ids das tabelas Pix também são
--   BIGSERIAL, diferente do Base Service: a 20 milhões de Pix por mês,
--   INT4 dura nove anos aqui e menos de dois na tabela de regras logo
--   abaixo, e trocar depois reescreve a tabela inteira com ela travada.
--
-- • transaction_date (e opening_date, em account) é TIMESTAMPTZ,
--   diferente do TIMESTAMP que o Base Service usa em created_at.
--   created_at é o relógio do nosso servidor; essas vêm de fora, com fuso
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
-- • external_id e device_id são opacos ("ID único", "ID do aparelho"), por
--   isso 255. ip em VARCHAR(45) cabe IPv6; INET daria operadores de rede
--   que nenhuma regra usa. latitude/longitude vêm as duas ou nenhuma, e
--   dentro da faixa: fora dela o erro seria do tipo, e viraria 500.
CREATE TABLE pix_transaction(
    id                              BIGSERIAL PRIMARY KEY,
    pix_transaction_key             CHAR(36) NOT NULL,
    status_id                       INTEGER NOT NULL REFERENCES pix_transaction_status(id),
    source_account_id               BIGINT NOT NULL REFERENCES account(id),
    destination_account_id          BIGINT NOT NULL REFERENCES account(id),
    pix_transaction_data            JSONB NOT NULL,
    external_id                     VARCHAR(255) NOT NULL,
    amount                          BIGINT NOT NULL,
    transaction_date                TIMESTAMPTZ NOT NULL,
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
-- Conta de origem: quantos Pix e quanto dinheiro em 1 min, 10 min, 1 h,
-- 24 h; valor e horário habituais; aparelho ou destino que esta conta
-- nunca usou. Esta última não precisa de índice pelo par (origem,
-- destino) enquanto o histórico de uma conta for pequeno; quando a regra
-- existir e doer, o índice entra com CREATE INDEX CONCURRENTLY — sem
-- travar a tabela — e não mexe em mais nada. A mesma pergunta por
-- documento soma as contas do customer, via account_customer_id_idx.
CREATE INDEX pix_transaction_source_account_id_idx
    ON pix_transaction (source_account_id, transaction_date);

-- Conta de destino: muitos pagadores distintos pro mesmo destino em
-- janela curta (robô multiplicador, loja falsa, conta laranja). Por conta
-- aqui; por documento é o JOIN com account → customer — o laranja de
-- customer, lá em cima.
CREATE INDEX pix_transaction_destination_account_id_idx
    ON pix_transaction (destination_account_id, transaction_date);

-- Aparelho: quantos Pix por device_id e aparelho compartilhado (um
-- aparelho pagando por vários documentos distintos é indício de farm de
-- contas).
CREATE INDEX pix_transaction_device_id_idx
    ON pix_transaction (device_id, transaction_date);
CREATE INDEX pix_transaction_ip_idx
    ON pix_transaction (ip, transaction_date);

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
    id BIGSERIAL PRIMARY KEY,
    pix_transaction_id BIGINT NOT NULL REFERENCES pix_transaction(id),
    status_id INTEGER NOT NULL REFERENCES pix_transaction_status(id),
    rule_result_data JSONB NOT NULL,
    rule_name VARCHAR(50) NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    UNIQUE (pix_transaction_id, rule_name),
    CHECK (rule_name <> ''),
    CHECK (jsonb_typeof(rule_result_data) = 'object')
);
