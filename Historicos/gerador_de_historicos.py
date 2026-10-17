"""Gera históricos Pix sintéticos, um arquivo JSON por cliente.

Exemplo:
	python gerador_de_historicos.py --clientes 10 --transacoes 50 \
		--aprovadas 0.7 --2fa 0.2 --reprovadas 0.1
"""

import argparse
import json
import random
import statistics
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path


STATUSES = ("approved", "two_factor_required", "rejected")
TIMEZONE = timezone(timedelta(hours=-3))


def generate_cpf(rng):
	digits = [rng.randrange(10) for _ in range(9)]

	for weight in (10, 11):
		total = sum(digit * factor for digit, factor in zip(digits, range(weight, 1, -1)))
		remainder = (total * 10) % 11
		digits.append(0 if remainder == 10 else remainder)

	return "{}.{}.{}-{}".format(
		"".join(map(str, digits[:3])),
		"".join(map(str, digits[3:6])),
		"".join(map(str, digits[6:9])),
		"".join(map(str, digits[9:])),
	)


def parse_proportions(args, parser):
	proportions = [args.aprovadas, args.duas_fa, args.reprovadas]
	if any(value < 0 for value in proportions):
		parser.error("as proporções não podem ser negativas")

	total = sum(proportions)
	if abs(total - 1) <= 1e-9:
		return [value / total for value in proportions]
	if abs(total - 100) <= 1e-7:
		return [value / total for value in proportions]
	parser.error("as proporções devem somar 1 ou 100")


def distribute_statuses(transaction_count, proportions, rng):
	exact_counts = [transaction_count * proportion for proportion in proportions]
	counts = [int(count) for count in exact_counts]
	remaining = transaction_count - sum(counts)
	order = sorted(
		range(len(STATUSES)),
		key=lambda index: exact_counts[index] - counts[index],
		reverse=True,
	)
	for index in order[:remaining]:
		counts[index] += 1

	statuses = [status for status, count in zip(STATUSES, counts) for _ in range(count)]
	rng.shuffle(statuses)
	return statuses


def generate_account(rng, reference_time, minimum_age_days=30, maximum_age_days=3650):
	return {
		"participant": "{:08d}".format(rng.randrange(100_000_000)),
		"branch": "{:04d}".format(rng.randrange(1, 10_000)),
		"account_number": str(rng.randrange(1, 10_000_000)),
		"opening_date": (
			reference_time - timedelta(days=rng.randint(minimum_age_days, maximum_age_days))
		).isoformat(),
		"owner": {"document_number": generate_cpf(rng)},
	}


def generate_history(statuses, rng, reference_time):
	customer_document = generate_cpf(rng)
	source_account = {
		"branch": "{:04d}".format(rng.randrange(1, 10_000)),
		"account_number": str(rng.randrange(1, 10_000_000)),
		"opening_date": (
			reference_time - timedelta(days=rng.randint(365, 3650))
		).isoformat(),
		"owner": {"document_number": customer_document},
	}
	familiar_destination = generate_account(rng, reference_time, 365, 3650)
	known_device = "dvc-{}".format(uuid.uuid4().hex[:12])
	known_ip = "198.51.100.{}".format(rng.randint(1, 254))
	latitude = round(rng.uniform(-33.75, 5.27), 6)
	longitude = round(rng.uniform(-73.99, -34.79), 6)
	transactions = []

	for status in statuses:
		amount = rng.randint(5_000, 25_000)
		device_id = known_device
		ip = known_ip
		if status == "two_factor_required":
			device_id = "dvc-{}".format(uuid.uuid4().hex[:12])
			ip = "203.0.113.{}".format(rng.randint(1, 254))
		elif status == "rejected":
			amount = rng.randint(1_000, 25_000)

		transactions.append(
			{
				"id": str(uuid.uuid4()),
				"amount": amount,
				"transaction_date": reference_time.isoformat(),
				"source_account": source_account,
				"destination_account": familiar_destination,
				"source": {
					"device_id": device_id,
					"channel": "app",
					"ip": ip,
					"geolocation": {
						"latitude": latitude,
						"longitude": longitude,
					},
				},
				"status": status,
			}
		)

	approved_amounts = [
		transaction["amount"]
		for transaction in transactions
		if transaction["status"] == "approved"
	]
	median_amount = statistics.median(approved_amounts) if approved_amounts else 15_000
	for transaction in transactions:
		if transaction["status"] == "two_factor_required":
			transaction["amount"] = max(100_001, int(median_amount * 6) + 1)

	return transactions


def assign_statuses_to_clients(client_count, transactions_per_client, proportions, rng):
	total_count = client_count * transactions_per_client
	statuses = distribute_statuses(total_count, proportions, rng)
	assignments = [[] for _ in range(client_count)]

	for status in statuses:
		eligible_clients = [
			index
			for index, client_statuses in enumerate(assignments)
			if len(client_statuses) < transactions_per_client
		]
		client_index = min(
			eligible_clients,
			key=lambda index: (
				assignments[index].count(status),
				len(assignments[index]),
				index,
			),
		)
		assignments[client_index].append(status)

	return assignments


def schedule_histories(histories, rng, reference_time, parser):
	cluster_time = reference_time - timedelta(hours=1)
	approved_clients = [
		(index, next((tx for tx in history if tx["status"] == "approved"), None))
		for index, history in enumerate(histories)
	]
	approved_clients = [(index, transaction) for index, transaction in approved_clients if transaction]
	rejected_transactions = [
		transaction
		for history in histories
		for transaction in history
		if transaction["status"] == "rejected"
	]
	prelude_ids = set()

	if rejected_transactions:
		if len(approved_clients) < 4:
			parser.error(
				"para emular rejeições pela regra D-REJ, são necessários quatro pagadores "
				"com transações aprovadas e ao menos uma transação reprovada; aumente "
				"--clientes/--transacoes ou ajuste as proporções"
			)

		suspicious_destination = generate_account(
			rng,
			reference_time,
			minimum_age_days=2,
			maximum_age_days=2,
		)
		prelude_transactions = []
		for index, (_, transaction) in enumerate(approved_clients[:4]):
			transaction["destination_account"] = suspicious_destination
			transaction["transaction_date"] = (
				cluster_time - timedelta(minutes=4 - index)
			).isoformat()
			prelude_transactions.append(transaction)
		prelude_ids = {id(transaction) for transaction in prelude_transactions}

		for index, transaction in enumerate(rejected_transactions):
			transaction["destination_account"] = suspicious_destination
			transaction["transaction_date"] = (
				cluster_time + timedelta(seconds=10 + index % 240)
			).isoformat()

	for history in histories:
		approved_transactions = [tx for tx in history if tx["status"] == "approved"]
		for index, transaction in enumerate(approved_transactions):
			if id(transaction) in prelude_ids:
				continue
			transaction["transaction_date"] = (
				cluster_time - timedelta(days=max(10, len(approved_transactions)) - index)
			).isoformat()

		for index, transaction in enumerate(
			tx for tx in history if tx["status"] == "two_factor_required"
		):
			transaction["transaction_date"] = (
				cluster_time + timedelta(minutes=10, seconds=index % 2_400)
			).isoformat()

		history.sort(key=lambda transaction: transaction["transaction_date"])


def create_parser():
	parser = argparse.ArgumentParser(description="Gera históricos sintéticos de transações Pix.")
	parser.add_argument("--clientes", type=int, required=True, help="quantidade de clientes")
	parser.add_argument(
		"--transacoes",
		type=int,
		required=True,
		help="quantidade de transações no histórico de cada cliente",
	)
	parser.add_argument("--aprovadas", type=float, required=True, help="proporção aprovada")
	parser.add_argument("--2fa", dest="duas_fa", type=float, required=True, help="proporção que exige 2FA")
	parser.add_argument("--reprovadas", type=float, required=True, help="proporção reprovada")
	parser.add_argument(
		"--saida",
		type=Path,
		default=Path(__file__).resolve().parent / "historicos_gerados",
		help="subdiretório de saída (padrão: Historicos/historicos_gerados)",
	)
	parser.add_argument("--seed", type=int, help="semente opcional para reproduzir os dados aleatórios")
	return parser


def main():
	parser = create_parser()
	args = parser.parse_args()
	if args.clientes < 1:
		parser.error("--clientes deve ser maior que zero")
	if args.transacoes < 0:
		parser.error("--transacoes não pode ser negativa")

	proportions = parse_proportions(args, parser)
	rng = random.Random(args.seed)
	reference_time = datetime.now(TIMEZONE)
	client_statuses = assign_statuses_to_clients(
		args.clientes,
		args.transacoes,
		proportions,
		rng,
	)
	histories = [
		generate_history(statuses, rng, reference_time)
		for statuses in client_statuses
	]
	schedule_histories(histories, rng, reference_time, parser)
	args.saida.mkdir(parents=True, exist_ok=True)

	for client_number, history in enumerate(histories, start=1):
		output_path = args.saida / "cliente_{:04d}.json".format(client_number)
		output_path.write_text(json.dumps(history, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

	print("{} histórico(s) salvo(s) em {}".format(args.clientes, args.saida))


if __name__ == "__main__":
	main()
