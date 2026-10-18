-- O antifraude Pix, nas tabelas de contrato, histórico e fontes:
--   pix_transaction_status       os três veredictos do contrato.
--   pix_signal_status            os três estados de um sinal.
--   customer                     quem paga ou recebe: um por CPF ou CNPJ (o
--                                owner do payload).
--   account                      uma conta de um customer, em qualquer banco.
--   pix_transaction              uma linha por Pix: payload + decisão
--                                explicada, com FK pra conta de origem e
--                                de destino.
--   pix_transaction_rule_result  uma linha por sinal por Pix: a lista de
--                                regras que a resposta devolve em
--                                metadata.signals.
-- Os índices são um por pergunta de regra (conta de origem, conta de
-- destino, aparelho), todos terminando em transaction_date, mais um em
-- account(customer_id) pra ir das contas ao documento.

-- As três decisões do contrato, e só elas. O Pix já nasce decidido (o
-- controller roda as regras antes do INSERT): não há pendente, e não há
-- pix_transaction_status_event como no Base Service porque nenhuma rota
-- muda a decisão. No dia em que o 2FA for confirmado pela API, a tabela
-- de eventos nasce junto com o teste dela.
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

-- Os três estados de um sinal. Uma regra dispara, não dispara, ou não
-- tinha histórico pra responder. Os dois últimos parecem a mesma coisa
-- e não são: falta de informação não é prova de que está tudo bem, e o
-- GET precisa dizer "esta regra não rodou" em vez de "não viu nada".
CREATE TABLE pix_signal_status(
    id                              SERIAL PRIMARY KEY,
    enumerator                      VARCHAR(50) NOT NULL,
    created_at                      TIMESTAMP NOT NULL DEFAULT(NOW()),
    UNIQUE(enumerator)
);

INSERT INTO pix_signal_status (enumerator) VALUES
('triggered'),
('not_triggered'),
('unavailable');

-- Quem paga ou recebe, um por documento. Separado de account porque um
-- documento tem contas em bancos diferentes: o laranja com três contas em
-- três bancos é um documento só, e as regras por pessoa agrupam por aqui.
--
-- É também a chave da trava do controller: um advisory lock transacional
-- pelo documento do pagador (pg_advisory_xact_lock), tomado antes dos
-- upserts e das contagens, na mesma transação do INSERT do Pix — a
-- estante da RFC da biblioteca: ninguém conta os livros sem segurar a
-- estante. A trava é pelo documento, e não por esta linha nem pela conta
-- de origem, por dois motivos: as regras do pagador contam pelo
-- documento, somando as contas (dois Pix do mesmo CPF por contas
-- diferentes também passam um de cada vez); e a linha só existe depois do
-- upsert — travando a linha, dois primeiros Pix do mesmo documento
-- atravessariam essa janela juntos. O resto da história mora no
-- CustomerRepository.lock_for_pix_document.
--
-- Ninguém cadastra customer nem account neste serviço. Os dois nascem na
-- primeira vez que aparecem num payload, por INSERT ... ON CONFLICT DO
-- NOTHING no controller — é isso que o resto deste arquivo chama de
-- upsert, e são até quatro por Pix: customer e account, origem e destino
-- — e a linha não muda depois.
--
-- document_number é só dígitos: 11 (CPF) ou 14 (CNPJ), sem máscara. O
-- UNIQUE nele é a chave do histórico, e "123.456.789-09" e "12345678909"
-- são o mesmo documento — se a máscara entrasse, virariam dois customers
-- e toda regra por documento contaria metade. O controller tira a máscara
-- antes de gravar e de consultar; o JSON Schema aceita as duas formas. O
-- CHECK é o que impede uma máscara de escapar e furar o agrupamento:
-- VARCHAR(14) sozinho ainda deixaria passar "123.456.789-0".
CREATE TABLE customer(
    id                              BIGSERIAL PRIMARY KEY,
    customer_key                    CHAR(36) NOT NULL,
    document_number                 VARCHAR(14) NOT NULL,
    created_at                      TIMESTAMP NOT NULL DEFAULT(NOW()),
    UNIQUE(customer_key),
    UNIQUE(document_number),
    CHECK(document_number ~ '^[0-9]{11}$' OR document_number ~ '^[0-9]{14}$')
);

-- Uma conta, de qualquer banco, de um customer. O titular faz parte da
-- identidade — o UNIQUE inclui customer_id —, e uma conta conjunta vira
-- uma linha por titular. Pagador e recebedor são o documento, e o
-- histórico vai da conta a ele por customer_id: com a conta presa ao
-- primeiro titular, o Pix do segundo entraria no histórico do primeiro.
-- Nenhuma regra conta por conta (a idade do P01 e do R01 sai da
-- opening_date do payload), e a divisão não custa nada ao motor.
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
    UNIQUE NULLS NOT DISTINCT (participant, branch, account_number, customer_id),
    CHECK(participant IS NULL OR participant ~ '^[0-9]{8}$')
);

-- Das contas ao documento: "muitos pagadores pro mesmo documento de
-- destino" e "quantas contas este documento já usou" são JOIN
-- account → customer, e passam por aqui. Não há índice em participant
-- nem em (branch, account_number) além do UNIQUE: o upsert procura pelas
-- quatro colunas dele, e o UNIQUE já é esse índice.
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
-- status_id é o veredicto; as sete colunas logo abaixo dele são como ele
-- foi tomado. Duas notas de risco, 0 a 100, uma pro pagador e uma pro
-- destino — a RFC chama cada uma de eixo (payer_risk, recipient_risk).
-- Quanto histórico havia pra sustentar cada nota (payer_sufficiency,
-- recipient_sufficiency: unavailable, low, medium, high) — sem histórico
-- a nota é baixa por falta de dado, e é isso que a coluna diz, pra
-- ninguém ler nota baixa como conta limpa. Que versão dos pesos e da
-- tabela de decisão estava valendo (policy_version). Qual linha da tabela
-- venceu (decision_rule: D-REJ-ATO, D-ATO-1, D-PAY...) e os motivos em texto
-- (reasons). São colunas, não um JSONB de análise, porque ajustar os
-- pesos depois, olhando o que aconteceu, é uma consulta — "qual era o
-- payer_risk dos Pix que viraram 2FA?" — e não deveria passar por
-- reasons->>'payer_risk'. Sem índice nelas: quem ajusta peso roda a
-- consulta de vez em quando e pode esperar; não é caminho do POST. Os
-- sinais, um a um, ficam em pix_transaction_rule_result, no fim do
-- arquivo.
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
--   INT4 dura nove anos aqui e menos de um ano na tabela de sinais logo
--   abaixo, e trocar depois reescreve a tabela inteira com ela travada.
--
-- • transaction_date (e opening_date, em account) é TIMESTAMPTZ,
--   diferente do TIMESTAMP que o Base Service usa em created_at.
--   created_at é o relógio do nosso servidor; essas vêm de fora, com fuso
--   ("2026-10-17T22:15:00-03:00"), e TIMESTAMP descartaria o -03:00 em
--   silêncio: as 22h15 de São Paulo virariam 22h15 de lugar nenhum e a
--   regra de horário noturno (20h às 6h) erraria em três horas. Repare
--   que TIMESTAMPTZ guarda o instante e não o fuso: o banco roda em UTC,
--   e toda regra de horário converte com AT TIME ZONE 'America/Sao_Paulo'
--   antes de olhar a hora (no exemplo, 1 em UTC, 22 em São Paulo).
--
-- • external_id e device_id são opacos ("ID único", "ID do aparelho"), por
--   isso 255. ip em VARCHAR(45) cabe IPv6; INET daria operadores de rede
--   que nenhuma regra usa. latitude/longitude vêm as duas ou nenhuma, e
--   dentro da faixa: fora dela o erro seria do tipo, e viraria 500.
--
-- • received_at é quando o Pix chegou aqui, pelo relógio do servidor.
--   Fica só pra auditoria: as janelas de histórico contam por
--   transaction_date, e o comentário dos índices, logo abaixo, diz por
--   quê.
CREATE TABLE pix_transaction(
    id                              BIGSERIAL PRIMARY KEY,
    pix_transaction_key             CHAR(36) NOT NULL,
    status_id                       INTEGER NOT NULL REFERENCES pix_transaction_status(id),
    payer_risk                      SMALLINT NOT NULL,
    recipient_risk                  SMALLINT NOT NULL,
    payer_sufficiency               VARCHAR(12) NOT NULL,
    recipient_sufficiency           VARCHAR(12) NOT NULL,
    policy_version                  VARCHAR(20) NOT NULL,
    decision_rule                   VARCHAR(20) NOT NULL,
    reasons                         JSONB NOT NULL,
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
    received_at                     TIMESTAMPTZ NOT NULL,
    created_at                      TIMESTAMP NOT NULL DEFAULT(NOW()),
    UNIQUE(pix_transaction_key),
    UNIQUE(external_id),
    CHECK(jsonb_typeof(pix_transaction_data) = 'object'),
    CHECK(payer_risk BETWEEN 0 AND 100),
    CHECK(recipient_risk BETWEEN 0 AND 100),
    CHECK(payer_sufficiency IN ('unavailable', 'low', 'medium', 'high')),
    CHECK(recipient_sufficiency IN ('unavailable', 'low', 'medium', 'high')),
    CHECK(policy_version <> ''),
    CHECK(decision_rule <> ''),
    CHECK(jsonb_typeof(reasons) = 'array'),
    CHECK(amount > 0),
    CHECK((latitude IS NULL) = (longitude IS NULL)),
    CHECK(latitude IS NULL OR latitude BETWEEN -90 AND 90),
    CHECK(longitude IS NULL OR longitude BETWEEN -180 AND 180)
);

-- Cada índice abaixo responde a uma pergunta de regra, e todos terminam
-- em transaction_date porque a pergunta é sempre "deste X, numa janela
-- de tempo". A janela conta em transaction_date, não em received_at nem
-- em created_at: histórico se conta no tempo do negócio — a data vem do
-- sistema do banco, e dez Pix de dez dias mandados hoje são dez dias, e
-- não uma rajada —, é o que deixa quem testa mandar a data no payload, e
-- um Pix que chegar atrasado continua no lugar certo.
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

-- Aparelho: quantos Pix por device_id, e aparelho compartilhado (um
-- aparelho pagando por vários documentos distintos é indício de farm de
-- contas). O IP fica sem índice: é lido no histórico do pagador, e não há
-- regra de velocity por IP no catálogo — quando houver, o índice volta
-- com ela.
CREATE INDEX pix_transaction_device_id_idx
    ON pix_transaction (device_id, transaction_date);

-- A explicação da decisão: uma linha por sinal avaliado, por Pix,
-- inclusive os unavailable. É daqui que sai metadata.signals da resposta
-- e é isto que a RFC promete: quais regras rodaram, o que cada uma viu e
-- quanto cada uma pesou.
--
-- Cada linha é um sinal; o veredicto é um só e mora em pix_transaction.
-- rule_name é o código da regra na tabela da RFC (seção 5: P03, R01,
-- REL02...); signal_status_id é o estado; points é quanto o sinal somou
-- ao eixo, e pode ser negativo (REL02 é -8). Repare que not_triggered e
-- unavailable gravam 0 os dois, e rule_result_data vazio ({}): o que os
-- separa é o status. family é a família que a resposta devolve (Perfil,
-- Velocity, Contraparte...), não o grupo de teto da RFC — somar points
-- por Pix não reproduz payer_risk, porque o teto é aplicado no motor.
--
-- Do sinal à decisão: soma por grupo de teto (as regras que olham o
-- mesmo padrão dividem um teto), pra que cinco regras vendo a mesma
-- rajada não contem cinco vezes; soma por eixo (payer_risk e
-- recipient_risk, 0 a 100); e a tabela de decisão, linha a linha, até
-- uma bater. Pesos, tetos e a tabela moram no motor e na RFC. O banco
-- guarda o que cada regra viu (rule_result_data: observado, limite,
-- janela) e o que disse (status e points) naquela hora, pra que o motivo
-- de ontem continue legível com os pesos de amanhã.
--
-- rule_name, axis e family são texto com CHECK, não FK pra tabela: regra
-- é código, nasce e muda no motor, e uma tabela populada por INSERT aqui
-- custaria um docker compose down -v a cada regra ou família nova. Só o
-- status do sinal tem tabela própria (pix_signal_status): é o status da
-- linha, o papel que status_id tem no Base Service; domínio fixo de
-- atributo, como axis aqui e as sufficiency em pix_transaction, é CHECK.
--
-- Append-only: ninguém dá UPDATE aqui, e o controller grava os sinais de
-- um Pix num INSERT só (uma ida ao banco por sinal custaria o dobro). O
-- UNIQUE diz que cada regra roda uma vez por análise e, de quebra, é o
-- índice que o GET usa pra montar a resposta. É a tabela que mais cresce
-- — uma linha por regra do catálogo em cada Pix, dois terços do disco — e
-- a primeira alavanca quando apertar é deixar de gravar os not_triggered
-- e devolvê-los no GET a partir do catálogo da policy_version: o schema
-- não muda.
CREATE TABLE pix_transaction_rule_result(
    id                              BIGSERIAL PRIMARY KEY,
    pix_transaction_id              BIGINT NOT NULL REFERENCES pix_transaction(id),
    signal_status_id                INTEGER NOT NULL REFERENCES pix_signal_status(id),
    rule_result_data                JSONB NOT NULL,
    rule_name                       VARCHAR(50) NOT NULL,
    axis                            VARCHAR(10) NOT NULL,
    family                          VARCHAR(30) NOT NULL,
    points                          SMALLINT NOT NULL,
    created_at                      TIMESTAMP NOT NULL DEFAULT(NOW()),
    UNIQUE(pix_transaction_id, rule_name),
    CHECK(rule_name <> ''),
    CHECK(axis IN ('payer', 'recipient')),
    CHECK(family <> ''),
    CHECK(points BETWEEN -100 AND 100),
    CHECK(jsonb_typeof(rule_result_data) = 'object')
);
