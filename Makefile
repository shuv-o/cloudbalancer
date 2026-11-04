.PHONY: help
.DEFAULT_GOAL := help

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| sort \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2}'

# ---------------------------------------------------------------------------
# Stack
# ---------------------------------------------------------------------------

build: ## Build all images, including Nginx with the traffic-status module
	docker compose build

up: ## Start everything
	docker compose up -d
	@echo
	@echo "  Control panel   http://localhost:8081"
	@echo "  Grafana         http://localhost:3000"
	@echo "  Prometheus      http://localhost:9090"

down: ## Stop everything, keeping data
	docker compose down

destroy: ## Stop everything and delete all volumes, including certificates
	docker compose down -v

restart: ## Restart every service
	docker compose restart

ps: ## Show what is running
	docker compose ps

logs: ## Tail all logs
	docker compose logs -f

logs-nginx: ## Tail the gateway's logs
	docker compose logs -f nginx

logs-celery: ## Tail the worker's logs, where deploys and ACME show up
	docker compose logs -f celery

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

init: ## First run on a new server: check config, build, bring everything up
	@if [ ! -f .env ]; then \
		cp .env.example .env && chmod 600 .env; \
		echo "Created .env from the example."; \
		echo "Edit it -- every value marked CHANGE_ME -- then run 'make init' again."; \
		exit 1; \
	fi
	@if grep -q CHANGE_ME .env; then \
		echo "These still hold the example value and must be set before deploying:"; \
		grep -n CHANGE_ME .env | sed 's/^/  /'; \
		exit 1; \
	fi
	@# deploy.sh rather than build + up: it waits for Django to report healthy
	@# before declaring success. The first boot applies every migration and
	@# collects static files before gunicorn binds, so anything that assumes a
	@# fixed delay races with it.
	./deploy/deploy.sh local --build
	@echo
	@echo "Up. The admin account came from ADMIN_USERNAME / ADMIN_PASSWORD in .env."
	@echo "Reach the panel from your own machine with:"
	@echo "  ssh -L 8081:localhost:8081 $$(whoami)@$$(hostname -I 2>/dev/null | awk '{print $$1}')"
	@echo "then open http://localhost:8081"

migrate: ## Apply database migrations
	docker compose exec django python manage.py migrate

makemigrations: ## Generate migrations after a model change
	docker compose exec django python manage.py makemigrations

superuser: ## Create an operator account
	docker compose exec django python manage.py createsuperuser

shell: ## Open a Django shell
	docker compose exec django python manage.py shell

seed: ## Load a demo topology pointing at the mock backends
	docker compose exec django python manage.py seed_demo

# ---------------------------------------------------------------------------
# Updating a server
# ---------------------------------------------------------------------------

update: ## Pull the latest code and roll it out, building here (run on the server)
	git pull --ff-only origin main
	./deploy/deploy.sh local --build

release: ## Roll out a release CI already built: make release TAG=sha-a1b2c3d
	@test -n "$(TAG)" || (echo "usage: make release TAG=sha-a1b2c3d" && exit 1)
	git fetch --quiet origin && git checkout --quiet --force $(TAG_REF)
	./deploy/deploy.sh $(TAG)

rollback: ## Go back to the release running before the current one
	@test -f .deployed-tag || (echo "No release recorded yet." && exit 1)
	@echo "Currently: $$(cat .deployed-tag)"
	@echo "Pass the tag to return to:  make release TAG=<older-tag>"

# `make release TAG=sha-abc1234` checks out the matching commit by default.
TAG_REF ?= $(patsubst sha-%,%,$(TAG))

# ---------------------------------------------------------------------------
# Gateway
# ---------------------------------------------------------------------------

deploy: ## Render the config from the database and reload Nginx
	docker compose exec django python manage.py deploy_config

config: ## Print the config a deploy would write, without writing it
	docker compose exec django python manage.py deploy_config --dry-run

check: ## Run nginx -t against the live configuration
	docker compose exec django python manage.py deploy_config --check

reload: ## Reload Nginx gracefully
	docker compose exec nginx nginx -s reload

cache-purge: ## Empty the whole cache
	docker compose exec nginx sh -c 'rm -rf /var/cache/nginx/*'
	docker compose exec nginx nginx -s reload

# ---------------------------------------------------------------------------
# Certificates
# ---------------------------------------------------------------------------

certs: ## List certificates and when they expire
	docker compose exec django python manage.py shell -c \
		"from apps.certificates.models import Certificate; \
		 [print(f'{c.domain.name:40} {c.status:10} {c.days_until_expiry} days') for c in Certificate.objects.all()]"

renew: ## Renew every certificate inside its renewal window
	docker compose exec celery celery -A config call certificates.renew_due

# ---------------------------------------------------------------------------
# Tests and benchmarks
# ---------------------------------------------------------------------------

test: ## Run the test suite
	docker compose exec django python manage.py test

test-local: ## Run the test suite without Docker, against SQLite
	cd management && DJANGO_SETTINGS_MODULE=config.settings.test python manage.py test

bench-up: ## Start the mock backends used by the benchmark harness
	docker compose -f docker-compose.yml -f docker-compose.bench.yml up -d

bench: ## Measure the gateway's added latency, direct vs through
	./benchmarks/compare.sh

bench-log: ## Read added latency straight out of the access log
	./benchmarks/added_latency_from_logs.sh

# ---------------------------------------------------------------------------
# Panel
# ---------------------------------------------------------------------------

panel-build: ## Rebuild the control panel bundle
	docker compose up --build panel
	docker compose restart nginx

panel-dev: ## Run the panel's dev server against a local Django
	cd frontend && npm run dev

# ---------------------------------------------------------------------------
# Control panel exposure
# ---------------------------------------------------------------------------

CA_DIR := gateway/nginx/client-ca
NAME   ?= operator

panel-ca: ## Create the CA that signs operator client certificates
	@test ! -f $(CA_DIR)/panel-ca.key || \
		(echo "A CA already exists. Delete $(CA_DIR)/panel-ca.* first if you really mean to replace it." && exit 1)
	openssl ecparam -genkey -name prime256v1 -out $(CA_DIR)/panel-ca.key
	chmod 600 $(CA_DIR)/panel-ca.key
	openssl req -x509 -new -key $(CA_DIR)/panel-ca.key -sha256 -days 3650 \
		-subj "/CN=CloudBalancer CA" -out $(CA_DIR)/panel-ca.pem
	@echo
	@echo "CA created. Two things to do:"
	@echo "  1. Point the panel's access policy at /etc/nginx/client-ca/panel-ca.pem"
	@echo "  2. Back up $(CA_DIR)/panel-ca.key somewhere safe — and nowhere else."
	@echo "     Anyone holding it can issue themselves access."

panel-cert: ## Issue an operator certificate: make panel-cert NAME=alice
	@test -f $(CA_DIR)/panel-ca.key || (echo "Run 'make panel-ca' first." && exit 1)
	openssl ecparam -genkey -name prime256v1 -out $(CA_DIR)/$(NAME).key
	openssl req -new -key $(CA_DIR)/$(NAME).key -subj "/CN=$(NAME)" -out $(CA_DIR)/$(NAME).csr
	openssl x509 -req -in $(CA_DIR)/$(NAME).csr \
		-CA $(CA_DIR)/panel-ca.pem -CAkey $(CA_DIR)/panel-ca.key -CAcreateserial \
		-days 825 -sha256 -out $(CA_DIR)/$(NAME).pem
	openssl pkcs12 -export -inkey $(CA_DIR)/$(NAME).key -in $(CA_DIR)/$(NAME).pem \
		-certfile $(CA_DIR)/panel-ca.pem -name "CloudBalancer — $(NAME)" \
		-out $(CA_DIR)/$(NAME).p12
	rm -f $(CA_DIR)/$(NAME).csr
	@echo
	@echo "Issued $(CA_DIR)/$(NAME).p12 — import it into the operator's browser."
	@echo "Send it over something that is not email, and delete it here afterwards."

panel-revoke: ## Show how to remove an operator's access
	@echo "This setup verifies against the CA rather than a revocation list, so"
	@echo "the way to remove one operator is to reissue the CA and everyone's"
	@echo "certificates. For a team where that is too coarse, keep an address"
	@echo "allowlist as the second control and disable the account in the panel:"
	@echo
	@echo "  docker compose exec django python manage.py shell -c \\"
	@echo "    \"from django.contrib.auth.models import User; \\"
	@echo "     User.objects.filter(username='$(NAME)').update(is_active=False)\""

panel-status: ## Show how the panel is currently exposed
	docker compose exec django python manage.py shell -c \
		"from apps.security.selectors import security_summary; \
		 import json; print(json.dumps(security_summary(), indent=2, default=str))"

bench-protection: ## Measure what the per-address protections cost per request
	./benchmarks/protection_overhead.sh
