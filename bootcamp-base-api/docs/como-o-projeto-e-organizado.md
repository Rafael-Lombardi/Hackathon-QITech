# Component map

- `src/app.py`: composition root. Registers middleware, routes, errors.
- `src/resources/`: HTTP boundary. Parse request, call controller, return response.
- `src/controllers/`: use-case and transaction orchestration.
- `src/repositories/`: database queries.
- `src/models/`: SQLAlchemy mappings.
- `src/database.py`: request-scoped lazy session.
- `src/middlewares/`: request id, logging, auth, session lifecycle.
- `src/errors/`: domain errors and HTTP translation.
- `database/database.sql`: initial PostgreSQL schema.

Middleware registration order in `app.py` is reverse execution order. Keep request context before logging, and session lifecycle inside auth.

Public routes: `/` and `/health_check`. All other routes require `INTERNAL-TOKEN`.
