# Gerador de históricos Pix

Este diretório contém `gerador_de_historicos.py`, um gerador de dados sintéticos para exercitar o motor antifraude. Ele cria um arquivo JSON por cliente e distribui transações aprovadas, com solicitação de 2FA e reprovadas em proporções configuráveis.

Os cenários são heurísticos e inspirados no catálogo de regras do projeto. Eles ajudam a testar regras e sequências, mas não são dados reais nem uma política antifraude calibrada.

## Requisitos

- Python 3.10 ou mais recente
- Nenhuma biblioteca externa

## Como executar

A partir da raiz do repositório:

```bash
python Hackathon-QITech/Historicos/gerador_de_historicos.py \
  --clientes 10 \
  --transacoes 50 \
  --aprovadas 0.7 \
  --2fa 0.2 \
  --reprovadas 0.1
```

Esse exemplo gera 10 arquivos com 50 transações cada, totalizando 500 transações: 350 aprovadas, 100 com 2FA e 50 reprovadas. Os status são distribuídos pelo conjunto de clientes, não calculados separadamente para cada arquivo.

As proporções podem ser frações decimais ou percentuais, mas precisam somar `1` ou `100`. Por exemplo, `0.7`, `0.2`, `0.1` equivale a `70`, `20`, `10`. Como a quantidade de transações é inteira, eventuais frações são arredondadas pelo método dos maiores restos; a soma final de status sempre corresponde ao total gerado.

Consulte todas as opções com:

```bash
python Hackathon-QITech/Historicos/gerador_de_historicos.py --help
```

## Arquivos gerados

Por padrão, os arquivos são gravados em `Hackathon-QITech/Historicos/historicos_gerados/`, com nomes como `cliente_0001.json`. Cada arquivo contém uma lista de transações daquele cliente. Para escolher outro diretório, passe `--saida`:

```bash
python Hackathon-QITech/Historicos/gerador_de_historicos.py \
  --clientes 10 --transacoes 50 \
  --aprovadas 70 --2fa 20 --reprovadas 10 \
  --saida ./dados/historicos
```

O programa cria o diretório se ele não existir, mas não remove arquivos antigos. Use um diretório vazio ou diferente ao gerar uma quantidade menor de clientes do que em uma execução anterior.

Cada transação contém os campos de entrada do PDF (`id`, `amount`, `transaction_date`, `source_account`, `destination_account` e `source`) e o campo adicional `status`, que registra o resultado histórico: `approved`, `two_factor_required` ou `rejected`. O `status` é um rótulo para os testes; não faz parte do payload de entrada do endpoint no PDF.

A conta pagadora e o documento do titular permanecem iguais dentro do arquivo do cliente. Documentos, contas, aparelhos, IPs, valores, horários e destinatários são sintéticos. Os IPs usados pertencem a faixas reservadas para documentação. Os CPFs são gerados com dígitos verificadores, sem consulta a cadastros; use os arquivos somente em ambientes de teste.

## Cenários representados

- **Aprovadas:** usam uma conta pagadora antiga, um dispositivo e IP recorrentes, valores usuais e, normalmente, um destino conhecido e antigo. As transações aprovadas são distribuídas por vários dias para formar histórico. A exceção são as primeiras quatro aprovações de pagadores distintos quando há rejeições: elas são direcionadas ao destinatário novo como precursores da rajada exigida por D-REJ.
- **2FA:** usam um dispositivo e um IP inéditos para o pagador e um valor maior que cinco vezes a mediana das transações aprovadas, com mínimo de R$ 1.000 (`100000` centavos), aproximando os sinais P03 e P06 e a decisão D-ATO-1. Para P06 ser aplicável, o cliente precisa ter ao menos 10 aprovações em um intervalo de pelo menos sete dias. Se o histórico não tiver evidência suficiente, o sinal não deve ser interpretado como disparado apenas por causa do rótulo.
- **Reprovadas:** compartilham um destinatário cuja conta foi aberta há dois dias. Quatro transações aprovadas de pagadores distintos preparam o histórico; em seguida, as reprovadas formam uma rajada para esse mesmo titular, aproximando R01, R02, R03 e a decisão D-REJ.

Para emular D-REJ, é necessário gerar ao menos uma transação reprovada e haver pelo menos quatro clientes com transações aprovadas. Caso contrário, o programa encerra com uma mensagem explicando a pré-condição. Aumente `--clientes` e/ou `--transacoes`, ou ajuste as proporções para que os quatro pagadores e a rejeição existam.

O campo `--seed` controla parte das escolhas pseudoaleatórias e pode ajudar a repetir cenários, mas não garante arquivos idênticos: os IDs UUID e o horário de referência também variam entre execuções.

```bash
python Hackathon-QITech/Historicos/gerador_de_historicos.py \
  --clientes 10 --transacoes 50 \
  --aprovadas 0.7 --2fa 0.2 --reprovadas 0.1 \
  --seed 42
```
