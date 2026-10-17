# Pix antifraud API

Base service for hackathon implementation.

## Run

```bash
docker compose up --build
```

API: `http://localhost:3000`

```bash
curl http://localhost:3000/health_check
```

Tests need API and database running:

```bash
pip install -r requirements-dev.txt
pytest
```

## Flow

`src/app.py` wires middleware, routes, and error handlers.

Request path: request id → log → internal token → DB context → resource.

Resource handles HTTP. Controller owns use-case rules. Repository owns SQLAlchemy queries. DTO owns response shape. Middleware opens/closes DB context; controller commits.

Challenge schema lives in `database/database.sql`. Pix route/rules are intentionally not implemented yet.
