.EXPORT_ALL_VARIABLES:

SHELL:=/bin/bash

mkfile_path := $(abspath $(lastword $(MAKEFILE_LIST)))
mkfile_dir := $(shell dirname $(mkfile_path) )
PROJECTNAME := arcane_tutor

GIT_ROOT := $(shell git rev-parse --show-toplevel)
GIT_SHA := $(shell git rev-parse HEAD 2>/dev/null || echo "unknown")
GIT_BRANCH := $(shell git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "unknown")
MAYBENORUN := $(shell if echo | xargs --no-run-if-empty >/dev/null 2>/dev/null; then echo "--no-run-if-empty"; else echo ""; fi)
BASE_COMPOSE := $(mkfile_dir)/docker-compose.yml
LINTABLE_DIRS := .

XPGDATABASE=magic
XPGPASSWORD=foopassword
XPGPORT=15432
XPGUSER=foouser
HOSTNAME := $(shell hostname)

html_files := $(shell find . -type f -name "*.html")
js_files := $(shell find . -type f -name "*.js" | grep -v node_modules)

S3_BUCKET=biblioplex

.PHONY: \
	/tmp/PIP_ACCESS_TOKEN \
	/tmp/PIP_INDEX_URL \
	/tmp/auth.toml \
	/tmp/pip.conf \
	beleren_font \
	build_images \
	check_env \
	compare-minification \
	coverage \
	dockerclean \
	down \
	ensure_black \
	ensure_isort \
	ensure_pylint \
	fonts \
	help \
	hlep \
	images \
	lint \
	mplantin_font \
	pull_images \
	reset \
	test \
	test-integration \
	test-unit \
	up

help: # @doc show this help and exit
	@python ./scripts/show_makefile_help.py $(mkfile_path)

hlep: help


###  Entry points

up_deps: datadir images check_env .env app.min.js

env.json: # @doc create env.json from template only if it does not exist (never overwrite)
	@test -f env.json || echo '{}' > env.json

.env: env.json
	cat env.json | jq -r 'to_entries[] | "\(.key)=\(.value)"' | sort > $@

dev-up: up_deps # @doc start services
	cd $(GIT_ROOT) && docker compose --profile=dev --file $(BASE_COMPOSE) up --remove-orphans --abort-on-container-exit

prod-up: up_deps # @doc start services
	cd $(GIT_ROOT) && docker compose --profile=prod --file $(BASE_COMPOSE) up --remove-orphans --abort-on-container-exit

prod-up-detach: up_deps
	cd $(GIT_ROOT) && docker compose --profile=prod --file $(BASE_COMPOSE) up --remove-orphans --detach

down: dev-down prod-down

dev-down: # @doc stop all services
	docker compose --profile=dev --file $(BASE_COMPOSE) down --remove-orphans > /dev/null

prod-down: # @doc stop all services
	docker compose --profile=prod --file $(BASE_COMPOSE) down --remove-orphans > /dev/null

images: build_images pull_images # @doc refresh images

build_images: # @doc refresh locally built images
	cd $(GIT_ROOT) && \
	docker compose --progress=plain --profile=dev --profile=prod --file $(BASE_COMPOSE) build

pull_images: $(BASE_COMPOSE) # @doc pull images from remote repos
	true || docker compose --file $(BASE_COMPOSE) pull

ensure_black: ensure_uv
	@python -m black --version > /dev/null || \
	python -m uv pip install black

ensure_isort: ensure_uv
	@python -m isort --version > /dev/null || \
	python -m uv pip install isort

ensure_pylint: ensure_uv
	@python -m pylint /dev/null || \
	python -m uv pip install pylint

ensure_pydocker: ensure_uv
	@python -c "import docker" 2>/dev/null || \
	python -m uv pip install docker

ensure_ruff: ensure_uv
	@python -m ruff --version > /dev/null || \
	python -m uv pip install ruff

ensure_uv:
	@python -m uv --version > /dev/null || \
	python -m pip install uv

lint: ruff_lint prettier_lint # @doc lint all python files
	true

prettier_lint: /tmp/prettier.stamp
	true

/tmp/prettier.stamp: $(html_files) $(js_files)
	npx prettier --write $(html_files) $(js_files)
	touch /tmp/prettier.stamp

ruff_fix: ensure_ruff
	git ls-files '*.py' | xargs python -m ruff check --fix --unsafe-fixes >/dev/null 2>/dev/null || true
	git ls-files '*.py' | xargs python -m ruff format

ruff_lint: ruff_fix
	git ls-files '*.py' | xargs python -m ruff check --fix --unsafe-fixes

# pylint_lint: ruff_fix ensure_pylint
# 	find . -type f -name "*.py" | xargs python -m pylint --fail-under 7.0 --max-line-length=132

check_env: ensure_pydocker
	true

dockerclean:
	docker ps --all --format '{{.ID}}' | xargs $(MAYBENORUN) docker stop
	docker ps --all --format '{{.ID}}' | xargs $(MAYBENORUN) docker rm --force
	docker images --format '{{.ID}}' | xargs $(MAYBENORUN) docker rmi --force

dbconn: # @doc connect to the local database

	@PGDATABASE=$(XPGDATABASE) \
	PGHOST=127.0.0.1 \
	PGPASSWORD=$(XPGPASSWORD) \
	PGPORT=25432 \
	PGUSER=$(XPGUSER) \
	$(shell find /usr/bin /opt/homebrew -name psql)

dump_schema: # @doc dump database schema to file using container's pg_dump
	docker exec $(PROJECTNAME)postgres $(shell find /usr/bin /opt/homebrew -name pg_dump) -U $(XPGUSER) -d $(XPGDATABASE) -s

datadir:
	bash ./scripts/make_datadirs.sh

reset:
	rm -rvf data

install_deps:
	python -m uv pip install -r requirements/base.txt

install_test_deps:
	python -m uv pip install -r requirements/test.txt -r requirements/base.txt

test tests: install_test_deps
	python -m pytest -vvv --capture=no --durations=10

test-integration:
	python -m pytest api/tests/test_integration_testcontainers.py -vvv --exitfirst

test-unit:
	python -m pytest -vvv --exitfirst --ignore=api/tests/test_integration_testcontainers.py

coverage: # @doc generate HTML coverage report
	python -m pytest --cov=. --cov-report=html --cov-report=term-missing --durations=10 -vvv

test-profiling:
	python -m pytest --profile-svg --durations=10 -vvv -k TestImportCardByName

font-dependencies:
	echo "Installing font subsetting dependencies..."
	python -m uv pip install -r requirements/fonts.txt

fonts: mana_font beleren_font mplantin_font

mana_font: font-dependencies # @doc subset and optimize the Mana font for web delivery
	python scripts/subset_mana_font.py \
		--output-dir data/fonts/mana \
		--cdn-url https://d1hot9ps2xugbc.cloudfront.net/cdn/fonts/mana \
		--s3-bucket $(S3_BUCKET) \
		--s3-prefix cdn/fonts/mana

beleren_font: font-dependencies # @doc subset and optimize the Beleren font for web delivery
	python scripts/subset_beleren_font.py \
		--output-dir data/fonts/beleren \
		--cdn-url https://d1hot9ps2xugbc.cloudfront.net/cdn/fonts/beleren \
		--s3-bucket $(S3_BUCKET) \
		--s3-prefix cdn/fonts/beleren

mplantin_font: font-dependencies # @doc subset and optimize the MPlantin font for web delivery
	python scripts/subset_mplantin_font.py \
		--input-font fonts/mplantin.otf \
		--output-dir data/fonts/mplantin \
		--cdn-url https://d1hot9ps2xugbc.cloudfront.net/cdn/fonts/mplantin \
		--s3-bucket $(S3_BUCKET) \
		--s3-prefix cdn/fonts/mplantin

compare-minification: # @doc compare file sizes: uncompressed, compressed, minified, and minified+compressed
	@echo "Installing minifier dependencies..."
	@npm install --no-save cssnano-cli terser > /dev/null 2>&1 || true
	@python scripts/compare_minification.py

app.min.js: api/static/app.js # @doc minify app.js (used in both dev and prod)
	@echo "Minifying app.js..."
	@npm install --no-save terser > /dev/null 2>&1 || true
	@npx terser api/static/app.js --compress --mangle --output api/static/app.min.js
	@echo "Created api/static/app.min.js"
