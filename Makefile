.PHONY: help setup up down stop logs shell migrate migrate-down test test-cov lint type-check \
        format security-scan generate-keys seed-admin seed-dev clean clean-db worker worker-beat \
        dev dev-all dev-check api

GIT_COMMIT = git -c user.email=dev@eclplatform.com -c user.name="ECL Developer" commit

VENV := .venv
VENV_BIN := $(VENV)/bin

$(VENV_BIN)/python:
	@echo "Creating virtualenv and installing dependencies..."
	python3 -m venv $(VENV)
	$(VENV_BIN)/pip install -e ".[dev]"

setup: $(VENV_BIN)/python ## First-time: create venv + install deps

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'

up: ## Start postgres and redis
	docker compose -f docker/docker-compose.yml up -d

down: ## Stop services
	docker compose -f docker/docker-compose.yml down

logs: ## Follow docker logs
	docker compose -f docker/docker-compose.yml logs -f

shell: ## Open psql shell
	docker compose -f docker/docker-compose.yml exec postgres psql -U ecl ecl_db

migrate: $(VENV_BIN)/python ## Apply migrations
	$(VENV_BIN)/alembic upgrade head

migrate-down: $(VENV_BIN)/python ## Rollback one migration
	$(VENV_BIN)/alembic downgrade -1

migrate-new: $(VENV_BIN)/python ## New migration (MSG="description")
	$(VENV_BIN)/alembic revision --autogenerate -m "$(MSG)"

test: $(VENV_BIN)/python ## Run tests
	$(VENV_BIN)/pytest tests/ -v --tb=short

test-cov: $(VENV_BIN)/python ## Tests with coverage
	$(VENV_BIN)/pytest tests/ -v --cov=app --cov-report=term-missing

test-auth: $(VENV_BIN)/python ## Auth tests only
	$(VENV_BIN)/pytest tests/test_auth/ -v

lint: $(VENV_BIN)/python ## Ruff check
	$(VENV_BIN)/ruff check app/ tests/

format: $(VENV_BIN)/python ## Ruff format
	$(VENV_BIN)/ruff format app/ tests/

type-check: $(VENV_BIN)/python ## Mypy strict
	$(VENV_BIN)/mypy app/

security-scan: $(VENV_BIN)/python ## Bandit SAST + pip-audit dependency CVE scan
	$(VENV_BIN)/bandit -r app/ -ll
	$(VENV_BIN)/pip-audit --desc

generate-keys: $(VENV_BIN)/python ## Generate RSA JWT keys
	$(VENV_BIN)/python scripts/generate_keys.py

seed-admin: $(VENV_BIN)/python ## Seed platform superadmin
	$(VENV_BIN)/python scripts/seed_superadmin.py

seed-dev: $(VENV_BIN)/python ## Seed dev data
	$(VENV_BIN)/python scripts/seed_dev_data.py

api: $(VENV_BIN)/python ## Start API server only (use when infra is already up)
	$(VENV_BIN)/uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

worker: $(VENV_BIN)/python ## Start Celery worker + beat — REQUIRED for email delivery (beat drains email_outbox) AND ECL compute
	@mkdir -p run
	$(VENV_BIN)/celery -A app.tasks.celery_app worker --beat --loglevel=info --concurrency=4 -n worker@%h --schedule=run/celerybeat-schedule

worker-beat: $(VENV_BIN)/python ## Start Celery worker + beat scheduler (periodic cleanup tasks)
	@mkdir -p run
	$(VENV_BIN)/celery -A app.tasks.celery_app worker --beat --loglevel=info --concurrency=4 -n worker@%h --schedule=run/celerybeat-schedule

stop: ## Stop stale API/Celery/honcho from previous dev runs
	@bash scripts/stop_dev.sh

dev: $(VENV_BIN)/python ## Docker + migrate + Celery (run `make api` in a second terminal)
	@bash scripts/dev.sh

dev-all: $(VENV_BIN)/python ## API + Celery together in one terminal (mixed logs)
	@bash scripts/stop_dev.sh
	@mkdir -p run
	$(VENV_BIN)/honcho start -f Procfile.dev

dev-check: $(VENV_BIN)/python ## Verify Redis is reachable on the configured Celery broker port
	@$(VENV_BIN)/python -c "from app.config import get_settings; import redis; s=get_settings(); r=redis.from_url(s.redis_celery_url); r.ping(); print('Redis OK:', s.redis_celery_url)"

clean: ## Remove caches
	find . -type f -name "*.pyc" -delete
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true

clean-db: $(VENV_BIN)/python ## Truncate all application data (keeps schema); also flushes Redis
	docker compose -f docker/docker-compose.yml up -d postgres redis
	$(VENV_BIN)/python scripts/clean_db.py --yes
	docker compose -f docker/docker-compose.yml exec -T redis redis-cli FLUSHDB
	@echo "Database and Redis cleared. Re-seed with: make seed-dev seed-admin"
