-- O antifraude Pix, nas tabelas de contrato, histórico e fontes:
--   pix_transaction_status       os três veredictos do contrato.
--   account                      identidade account-scoped: documento + conta.
--   pix_transaction              uma linha por Pix: payload + decisão
--                                explicada, com FK pra conta de origem e
--                                de destino.
-- Os índices são um por pergunta de regra (conta de origem, conta de
-- destino, aparelho), todos terminando em transaction_date, mais um em
-- account(document_number) para localizar a identidade.

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

-- A conta é a unidade de identidade do domínio. O controller serializa o
-- documento do pagador com advisory lock, faz o upsert das contas e trava
-- as contas envolvidas em ordem determinística antes de contar histórico.
--
-- document_number é só dígitos: 11 (CPF) ou 14 (CNPJ), sem máscara. O
-- UNIQUE nele é a chave do histórico, e "123.456.789-09" e "12345678909"
-- são o mesmo documento — se a máscara entrasse, virariam duas identidades
-- e toda regra por documento contaria metade. O controller tira a máscara
-- antes de gravar e de consultar; o JSON Schema aceita as duas formas. O
-- CHECK é o que impede uma máscara de escapar e furar o agrupamento:
-- VARCHAR(14) sozinho ainda deixaria passar "123.456.789-0".
-- Uma linha combina o documento e a conta concreta usada no Pix.
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
    document_number                 VARCHAR(14) NOT NULL,
    participant                     CHAR(8),
    branch                          VARCHAR(10) NOT NULL,
    account_number                  VARCHAR(20) NOT NULL,
    opening_date                    TIMESTAMPTZ NOT NULL,
    created_at                      TIMESTAMP NOT NULL DEFAULT(NOW()),
    UNIQUE(account_key),
    UNIQUE NULLS NOT DISTINCT (participant, branch, account_number, document_number),
    CHECK(participant IS NULL OR participant ~ '^[0-9]{8}$'),
    CHECK(document_number ~ '^[0-9]{11}$' OR document_number ~ '^[0-9]{14}$')
);

-- O índice permite localizar rapidamente todas as linhas account-scoped do
-- documento. Não há índice em participant
-- nem em (branch, account_number) além do UNIQUE: o upsert procura pelas
-- quatro colunas dele, e o UNIQUE já é esse índice.
CREATE INDEX account_document_number_idx
    ON account (document_number);

-- Cadastro independente de aparelhos confiáveis. Pix aprovado não insere
-- aqui: a linha só vem de um seed/serviço de identidade e pode ser revogada.
CREATE TABLE device_registry(
    id                              BIGSERIAL PRIMARY KEY,
    account_id                      BIGINT NOT NULL REFERENCES account(id),
    device_id                      VARCHAR(255) NOT NULL,
    registered_at                  TIMESTAMPTZ NOT NULL,
    revoked_at                     TIMESTAMPTZ,
    provenance                     VARCHAR(50) NOT NULL,
    created_at                     TIMESTAMP NOT NULL DEFAULT(NOW()),
    UNIQUE(account_id, device_id),
    CHECK(revoked_at IS NULL OR revoked_at > registered_at),
    CHECK(provenance <> '')
);

CREATE INDEX device_registry_account_device_idx
    ON device_registry (account_id, device_id);

-- Fontes autoritativas seedadas para a política v0.5. Não são histórico de
-- Pix: representam o snapshot recebido de listas de restrição e inteligência
-- externa no instante da avaliação. occurrence_id torna replays idempotentes.
CREATE TABLE risk_restriction(
    id                              BIGSERIAL PRIMARY KEY,
    scope                           VARCHAR(30) NOT NULL,
    account_id                     BIGINT NOT NULL REFERENCES account(id),
    active_from                    TIMESTAMPTZ NOT NULL,
    active_until                   TIMESTAMPTZ,
    status                          VARCHAR(20) NOT NULL DEFAULT('active'),
    provenance                     VARCHAR(80) NOT NULL,
    occurrence_id                  VARCHAR(255) NOT NULL UNIQUE,
    created_at                     TIMESTAMP NOT NULL DEFAULT(NOW()),
    CHECK(scope IN ('payer', 'recipient_holder', 'recipient_account')),
    CHECK(account_id IS NOT NULL),
    CHECK(active_until IS NULL OR active_until > active_from),
    CHECK(status IN ('active', 'revoked')),
    CHECK(provenance <> '')
);

CREATE INDEX risk_restriction_account_idx
    ON risk_restriction (account_id, active_from);

CREATE TABLE external_risk_assessment(
    id                              BIGSERIAL PRIMARY KEY,
    account_id                     BIGINT NOT NULL REFERENCES account(id),
    risk_subject                    VARCHAR(10) NOT NULL,
    assessment                     VARCHAR(30) NOT NULL,
    valid_from                     TIMESTAMPTZ NOT NULL,
    valid_until                    TIMESTAMPTZ,
    provenance                     VARCHAR(80) NOT NULL,
    occurrence_id                  VARCHAR(255) NOT NULL UNIQUE,
    severity                       VARCHAR(20),
    source_version                 VARCHAR(50),
    observed_at                    TIMESTAMPTZ,
    created_at                     TIMESTAMP NOT NULL DEFAULT(NOW()),
    CHECK(risk_subject IN ('payer', 'recipient')),
    CHECK(assessment IN ('clear', 'suspicious', 'confirmed_in_mock', 'unavailable')),
    CHECK(valid_until IS NULL OR valid_until > valid_from),
    CHECK(provenance <> '')
);

CREATE INDEX external_risk_account_axis_idx
    ON external_risk_assessment (account_id, risk_subject, valid_from);

-- Uma linha por Pix recebido: o fato observado. A decisão fica em
-- pix_analysis, uma linha 1:1, para separar entrada de resultado.
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
-- destino — cada uma funciona como um eixo (payer_risk, recipient_risk).
-- Quanto histórico havia pra sustentar cada nota (payer_sufficiency,
-- recipient_sufficiency: unavailable, low, medium, high) — sem histórico
-- a nota é baixa por falta de dado, e é isso que a coluna diz, pra
-- ninguém ler nota baixa como conta limpa. Que versão dos pesos e da
-- tabela de decisão estava valendo (policy_version). Qual linha da tabela
-- venceu (decision_rule: D-REJ, D-ATO-1, D-PAY...) e os motivos em texto
-- (reasons). São colunas, não um JSONB de análise, porque ajustar os
-- pesos depois, olhando o que aconteceu, é uma consulta — "qual era o
-- payer_risk dos Pix que viraram 2FA?" — e não deveria passar por
-- reasons->>'payer_risk'. Sem índice nelas: quem ajusta peso roda a
-- consulta de vez em quando e pode esperar; não é caminho do POST. Os
-- a explicação detalhada fica em pix_analysis.metadata.
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
CREATE TABLE pix_transaction(
    id                              BIGSERIAL PRIMARY KEY,
    pix_transaction_key             CHAR(36) NOT NULL,
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
    CHECK(amount > 0),
    CHECK((latitude IS NULL) = (longitude IS NULL)),
    CHECK(latitude IS NULL OR latitude BETWEEN -90 AND 90),
    CHECK(longitude IS NULL OR longitude BETWEEN -180 AND 180)
);

-- Cada índice abaixo responde a uma pergunta de regra, e todos terminam
-- em received_at porque a pergunta é sempre "deste X, numa janela de
-- tempo". O relógio do histórico é o recebimento do servidor; o instante
-- de avaliação monotônico é calculado depois das travas.
--
-- Conta de origem: quantos Pix e quanto dinheiro em 1 min, 10 min, 1 h,
-- 24 h; valor e horário habituais; aparelho ou destino que esta conta
-- nunca usou. Esta última não precisa de índice pelo par (origem,
-- destino) enquanto o histórico de uma conta for pequeno; quando a regra
-- existir e doer, o índice entra com CREATE INDEX CONCURRENTLY — sem
-- travar a conta — e não mexe em mais nada. A pergunta por documento é
-- respondida diretamente por account.document_number.
CREATE INDEX pix_transaction_source_account_id_idx
    ON pix_transaction (source_account_id, received_at);

-- Conta de destino: muitos pagadores distintos pro mesmo destino em
-- janela curta (robô multiplicador, loja falsa, conta laranja). A consulta
-- usa a conta concreta indicada no Pix.
CREATE INDEX pix_transaction_destination_account_id_idx
    ON pix_transaction (destination_account_id, received_at);

-- Aparelho: quantos Pix por device_id, e aparelho compartilhado (um
-- aparelho pagando por vários documentos distintos é indício de farm de
-- contas). O IP fica sem índice: é lido no histórico do pagador, e não há
-- regra de velocity por IP no catálogo — quando houver, o índice volta
-- com ela.
CREATE INDEX pix_transaction_device_id_idx
    ON pix_transaction (device_id, received_at);

-- Uma única decisão por Pix. metadata guarda a explicação compacta (sinais,
-- evidências consultadas e contexto) sem criar uma tabela por regra.
CREATE TABLE pix_analysis(
    id                              BIGSERIAL PRIMARY KEY,
    pix_transaction_id              BIGINT NOT NULL REFERENCES pix_transaction(id),
    status_id                       INTEGER NOT NULL REFERENCES pix_transaction_status(id),
    payer_risk                      SMALLINT NOT NULL,
    recipient_risk                  SMALLINT NOT NULL,
    payer_sufficiency               VARCHAR(12) NOT NULL,
    recipient_sufficiency           VARCHAR(12) NOT NULL,
    policy_version                  VARCHAR(20) NOT NULL,
    decision_rule                   VARCHAR(20) NOT NULL,
    reasons                         JSONB NOT NULL,
    evaluated_at                    TIMESTAMPTZ NOT NULL,
    metadata                        JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at                      TIMESTAMP NOT NULL DEFAULT(NOW()),
    UNIQUE(pix_transaction_id),
    CHECK(payer_risk BETWEEN 0 AND 100),
    CHECK(recipient_risk BETWEEN 0 AND 100),
    CHECK(payer_sufficiency IN ('unavailable', 'low', 'medium', 'high')),
    CHECK(recipient_sufficiency IN ('unavailable', 'low', 'medium', 'high')),
    CHECK(policy_version <> ''),
    CHECK(decision_rule <> ''),
    CHECK(jsonb_typeof(reasons) = 'array'),
    CHECK(jsonb_typeof(metadata) = 'object')
);

-- A explicação da decisão fica em pix_analysis.metadata. Não há uma linha
-- por sinal nem uma tabela separada de snapshots: isso mantém o modelo
-- pequeno para o escopo síncrono do desafio.
/* A explicação da decisão: uma linha por sinal avaliado, por Pix,
-- inclusive os unavailable. É daqui que sai metadata.signals da resposta
-- e é isto que o contrato promete: quais regras rodaram, o que cada uma viu e
-- quanto cada uma pesou.
--
-- Cada linha é um sinal; o veredicto é um só e mora em pix_transaction.
-- rule_name é o código da regra no catálogo (P03, R01,
-- REL02...); signal_status_id é o estado; points é quanto o sinal somou
-- ao eixo, e pode ser negativo (REL02 é -8). Repare que not_triggered e
-- unavailable gravam 0 os dois, e rule_result_data vazio ({}): o que os
-- separa é o status. family é a família que a resposta devolve (Perfil,
-- Velocity, Contraparte...), não o grupo de teto — somar points
-- por Pix não reproduz payer_risk, porque o teto é aplicado no motor.
--
-- Do sinal à decisão: soma por grupo de teto (as regras que olham o
-- mesmo padrão dividem um teto), pra que cinco regras vendo a mesma
-- rajada não contem cinco vezes; soma por eixo (payer_risk e
-- recipient_risk, 0 a 100); e a tabela de decisão, linha a linha, até
-- uma bater. Pesos, tetos e a tabela moram no motor e no contrato. O banco
-- guarda o que cada regra viu (rule_result_data: observado, limite,
-- janela) e o que disse (status e points) naquela hora, pra que o motivo
-- de ontem continue legível com os pesos de amanhã.
--
-- rule_name, risk_subject e family são texto com CHECK, não FK pra tabela: regra
-- é código, nasce e muda no motor, e uma tabela populada por INSERT aqui
-- custaria um docker compose down -v a cada regra ou família nova. Só o
-- os estados individuais dos sinais ficam no metadata da análise; não há
-- catálogo ou tabela de resultado por regra.
--
-- Append-only: ninguém dá UPDATE aqui, e o controller grava os sinais de
-- um Pix num INSERT só (dezenove idas ao banco custam o dobro de uma). O
-- UNIQUE diz que cada regra roda uma vez por análise e, de quebra, é o
-- índice que o GET usa pra montar a resposta. É a tabela que mais cresce
-- — dezenove linhas por Pix, dois terços do disco — e a primeira alavanca
-- quando apertar é deixar de gravar os not_triggered e devolvê-los no GET
-- a partir do catálogo da policy_version: o schema não muda.
CREATE TABLE pix_transaction_rule_result(
    id                              BIGSERIAL PRIMARY KEY,
    pix_transaction_id              BIGINT NOT NULL REFERENCES pix_transaction(id),
    signal_status_id                INTEGER NOT NULL REFERENCES pix_signal_status(id),
    rule_result_data                JSONB NOT NULL,
    rule_name                       VARCHAR(50) NOT NULL,
    risk_subject                    VARCHAR(10) NOT NULL,
    family                          VARCHAR(30) NOT NULL,
    points                          SMALLINT NOT NULL,
    created_at                      TIMESTAMP NOT NULL DEFAULT(NOW()),
    UNIQUE(pix_transaction_id, rule_name),
    CHECK(rule_name <> ''),
    CHECK(risk_subject IN ('payer', 'recipient')),
    CHECK(family <> ''),
    CHECK(points BETWEEN -100 AND 100),
    CHECK(jsonb_typeof(rule_result_data) = 'object')
);

-- Snapshot imutável das restrições e assessments que o controller consultou
-- para este Pix. A fonte pode mudar depois; a análise continua auditável.
CREATE TABLE pix_transaction_risk_evidence(
    id                              BIGSERIAL PRIMARY KEY,
    pix_transaction_id              BIGINT NOT NULL REFERENCES pix_transaction(id),
    source_type                    VARCHAR(30) NOT NULL,
    risk_subject                    VARCHAR(10),
    scope                           VARCHAR(30),
    assessment                     VARCHAR(30),
    provenance                     VARCHAR(80) NOT NULL,
    occurrence_id                  VARCHAR(255) NOT NULL,
    valid_from                     TIMESTAMPTZ NOT NULL,
    valid_until                    TIMESTAMPTZ,
    captured_at                    TIMESTAMPTZ NOT NULL,
    severity                       VARCHAR(20),
    source_version                 VARCHAR(50),
    observed_at                    TIMESTAMPTZ,
    created_at                     TIMESTAMP NOT NULL DEFAULT(NOW()),
    UNIQUE(pix_transaction_id, source_type, occurrence_id),
    CHECK(source_type IN ('restriction', 'dict_security')),
    CHECK(risk_subject IS NULL OR risk_subject IN ('payer', 'recipient')),
    CHECK(source_type <> 'restriction' OR scope IS NOT NULL),
    CHECK(source_type <> 'dict_security' OR risk_subject IS NOT NULL),
    CHECK(provenance <> ''),
    CHECK(occurrence_id <> ''),
    CHECK(valid_until IS NULL OR valid_until > valid_from)
);

CREATE INDEX pix_transaction_risk_evidence_transaction_idx
    ON pix_transaction_risk_evidence (pix_transaction_id);
*/
