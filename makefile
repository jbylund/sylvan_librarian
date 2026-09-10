ifndef MAKEFLAGS
# nproc is GNU coreutils and is absent on a stock macOS; sysctl is the BSD
# equivalent. Without a fallback CPUS is empty, which turns the flags below into
# a bare "-j -l" — unlimited parallel jobs with no load limit.
CPUS ?= $(shell nproc 2>/dev/null || sysctl -n hw.ncpu 2>/dev/null || echo 1)
MAKEFLAGS += -j $(CPUS) -l $(CPUS) -s
$(info Note: running on $(CPUS) CPU cores by default, use flag -j to override.)
endif

.EXPORT_ALL_VARIABLES:

SHELL:=/bin/bash

mkfile_path := $(abspath $(lastword $(MAKEFILE_LIST)))
mkfile_dir := $(shell dirname $(mkfile_path) )
PROJECTNAME := sylvan_librarian

# macOS ships python3 but no bare "python", so every recipe that shells out to
# the interpreter has to go through this rather than assume a name.
PYTHON := $(shell command -v python 2>/dev/null || command -v python3 2>/dev/null)

GIT_ROOT := $(shell git rev-parse --show-toplevel)
GIT_SHA := $(shell git rev-parse HEAD 2>/dev/null || echo "unknown")
GIT_BRANCH := $(shell git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "unknown")
MAYBENORUN := $(shell if echo | xargs --no-run-if-empty >/dev/null 2>/dev/null; then echo "--no-run-if-empty"; else echo ""; fi)
BASE_COMPOSE := $(mkfile_dir)/docker-compose.yml
DEV_COMPOSE := $(mkfile_dir)/docker-compose.dev.yml
# The --file arguments for one stack. Only dev layers the override that bind-mounts the host
# checkout's static files; blue and green serve the copy built into the image, otherwise a deploy
# would have the old API serving the new JS (and both stacks always serving the same files).
compose_files = --file $(BASE_COMPOSE)$(if $(filter dev,$(1)), --file $(DEV_COMPOSE))
# The two per-host env files every compose invocation reads before the stack's own envs/<stack>.
# They are separate files because they have separate writers: .env is rebuilt from env.json by a
# truncating make rule, .env.generated is written by scripts/gen_postgres_conf.py. Later files
# win, so a POSTGRES_MEM_LIMIT stranded in an older .env is overridden by the generated one.
COMPOSE_ENV_FILES := --env-file .env --env-file .env.generated
ENVS := $(shell ls envs)
LINTABLE_DIRS := .

# Local credentials come from env.json, created here on first run so that a fresh
# clone has everything it needs to boot. Read them back into make variables (which
# .EXPORT_ALL_VARIABLES puts in every recipe's environment) so the values compose
# sees match .env exactly — compose gives the shell environment precedence over
# --env-file, so any disagreement means env.json is silently ignored.
$(shell $(GIT_ROOT)/scripts/gen_env_json.sh $(GIT_ROOT)/env.json)

XPGDATABASE := $(shell jq -r '.XPGDATABASE' $(GIT_ROOT)/env.json)
XPGPASSWORD := $(shell jq -r '.XPGPASSWORD' $(GIT_ROOT)/env.json)
XPGUSER := $(shell jq -r '.XPGUSER' $(GIT_ROOT)/env.json)
HOSTNAME := $(shell hostname)

S3_BUCKET=biblioplex

html_files := $(shell git ls-files "*.html")
js_files := $(shell git ls-files "*.js")

requirements_sources := $(shell find requirements -type f -name "*.txt")
PYTHON_DIRS := $(shell git ls-files "*.py" | cut -f 1 -d/ | sort -u)
python_sources := $(shell find api client -type f -name "*.py")
engine_sources := $(shell find card_engine/src -type f -name "*.rs") card_engine/Cargo.toml card_engine/Cargo.lock card_engine/pyproject.toml
ENGINE_EXT_SUFFIX := $(shell $(PYTHON) -c "import sysconfig; print(sysconfig.get_config_var('EXT_SUFFIX'))")
ENGINE_SO := card_engine/card_engine/card_engine$(ENGINE_EXT_SUFFIX)
image_sources := $(python_sources) api/Dockerfile client/Dockerfile $(requirements_sources) $(BASE_COMPOSE) $(DEV_COMPOSE)

BUILD_STAMP_DIR := $(GIT_ROOT)/.tmp/build-stamps
BUILD_HASH := $(shell { git rev-parse HEAD 2>/dev/null; git diff origin/main 2>/dev/null; } | md5sum | cut -d' ' -f1)
BUILD_STAMP := $(BUILD_STAMP_DIR)/$(BUILD_HASH).stamp
IMAGE_TAG := $(BUILD_HASH)

.PHONY: \
	beleren_font \
	build_images \
	check_env \
	compare-minification \
	coverage \
	dockerclean \
	down \
	engine \
	fonts \
	help \
	hlep \
	images \
	js-deps \
	lint \
	mplantin_font \
	postgres-config \
	prettier_check \
	psql-dotfiles \
	pull_images \
	reset \
	rolling-deploy \
	status \
	test \
	test-integration \
	test-unit

postgres-config: configs/postgres/conf/postgresql.conf # @doc generate postgresql.conf from template scaled to available memory

# One command producing two files. GNU make 4.3's grouped targets (&:) would say that
# directly, but macOS ships make 3.81, so instead the conf is generated from its own sources
# and .env.generated is generated from the conf. In a run that rebuilds the conf, the recipe
# writes .env.generated too, leaving it newer than the conf, so the second rule finds it up to
# date and does not run twice. It fires on its own only when .env.generated is missing —
# deleted by hand, or a checkout that predates it.
define gen_postgres_conf
$(PYTHON) scripts/gen_postgres_conf.py \
	--template configs/postgres/conf/postgresql.conf.template \
	--output configs/postgres/conf/postgresql.conf \
	--env-output .env.generated
endef

configs/postgres/conf/postgresql.conf: configs/postgres/conf/postgresql.conf.template scripts/gen_postgres_conf.py
	$(gen_postgres_conf)

.env.generated: configs/postgres/conf/postgresql.conf
	$(gen_postgres_conf)

help: # @doc show this help and exit
	@$(PYTHON) ./scripts/show_makefile_help.py $(mkfile_path)

hlep: help


###  Entry points

up_deps: images check_env .env .env.generated api/static/app.min.js configs/postgres/conf/postgresql.conf

deps-%: up_deps psql-dotfiles
	mkdir -p $(GIT_ROOT)/data/api/$* && chmod 755 $(GIT_ROOT)/data/api/$*

# Both files are bind-mounted into the postgres container. Docker silently creates
# a directory in place of a missing bind-mount source, so on a host that has never
# run psql they turn into directories rather than files.
psql-dotfiles:
	@touch ~/.psqlrc ~/.psql_history

env.json: # @doc create env.json with generated local credentials if missing (never overwrite)
	@$(GIT_ROOT)/scripts/gen_env_json.sh $@

.env: env.json
	cat env.json | jq -r 'to_entries[] | "\(.key)=\(.value)"' | sort > $@

%-up: deps-% # @doc start an environment in the foreground, e.g. make dev-up
	cd $(GIT_ROOT) && docker compose --project-name sylvan_$* $(COMPOSE_ENV_FILES) --env-file envs/$* $(call compose_files,$*) up --remove-orphans --abort-on-container-exit

%-up-detach: deps-% # @doc start an environment in the background, e.g. make dev-up-detach
	cd $(GIT_ROOT) && docker compose --project-name sylvan_$* $(COMPOSE_ENV_FILES) --env-file envs/$* $(call compose_files,$*) up --remove-orphans --detach

%-down: | .env .env.generated # @doc stop an environment, e.g. make dev-down
	cd $(GIT_ROOT) && docker compose --project-name sylvan_$* $(COMPOSE_ENV_FILES) --env-file envs/$* $(call compose_files,$*) down --remove-orphans

status: | .env .env.generated # @doc show container status for all environments
	@$(foreach env,$(ENVS), \
	  $(PYTHON) -c "import shutil; w=shutil.get_terminal_size().columns; print(' $(env) '.center(w, '='))" && \
	  cd $(GIT_ROOT) && docker compose --project-name sylvan_$(env) $(COMPOSE_ENV_FILES) --env-file envs/$(env) $(call compose_files,$(env)) ps --all ; \
	)

# Hit the freshly started stack from the host, the way the reverse proxy will. `up --wait` only
# proves the container's own healthcheck passed; a bad published port, a proxy-facing bind address,
# or an engine that loads but cannot search would all still take green down with it. The port comes
# from the stack's env file and the host side of the published port defaults as docker-compose.yml
# does. `set -e` so a failing curl aborts the recipe (and, for blue, the whole deploy).
define smoke_check
set -e; port=$$(grep -E '^API_PORT=' envs/$(1) | cut -d= -f2); host=$${BIND_ADDR:-127.0.0.1}; \
for path in ready 'search?q=t:elf'; do \
  curl --fail --silent --show-error --max-time 30 --user-agent deploy-smoke --output /dev/null "http://$$host:$${port:-28080}/$$path" \
    || { echo "=== $(1): smoke check on /$$path failed, aborting deploy"; exit 1; }; \
done; echo "=== $(1): /ready and /search answered"
endef

rolling-deploy: pull_images deps-blue deps-green # @doc rolling blue/green deploy — update blue (wait for healthy + smoke check), then green
	@echo "=== Deploying blue ==="
	cd $(GIT_ROOT) && docker compose --project-name sylvan_blue $(COMPOSE_ENV_FILES) --env-file envs/blue $(call compose_files,blue) up --remove-orphans --detach --wait
	cd $(GIT_ROOT) && $(call smoke_check,blue)
	@echo "=== Blue healthy. Deploying green ==="
	cd $(GIT_ROOT) && docker compose --project-name sylvan_green $(COMPOSE_ENV_FILES) --env-file envs/green $(call compose_files,green) up --remove-orphans --detach --wait
	cd $(GIT_ROOT) && $(call smoke_check,green)
	@echo "=== Rolling deploy complete ==="

down: $(addsuffix -down,$(ENVS)) # @doc stop every environment

# Pulling is deliberately not part of `images` (and so not of every `make *-up`): the postgres:18
# tag only moves on a point release, and a registry round-trip on each start is not worth it. The
# deploy targets pull, so a deploy is what picks up a new point release.
images: build_images # @doc refresh locally built images

build_images: $(BUILD_STAMP) # @doc refresh locally built images

$(BUILD_STAMP): $(image_sources) | .env .env.generated
	mkdir -p $(BUILD_STAMP_DIR)
	find $(BUILD_STAMP_DIR) -name "*.stamp" -mtime +3 -delete 2>/dev/null || true
	cd $(GIT_ROOT) && docker compose --progress=plain $(COMPOSE_ENV_FILES) --env-file envs/dev --file $(BASE_COMPOSE) build
	touch $@

# Only the services that come from a registry: apiservice and client are built here and marked
# pull_policy: never, so a bare `pull` would try (and fail) to fetch them.
pull_images: | .env .env.generated # @doc pull the postgres image (a moving major-version tag)
	cd $(GIT_ROOT) && docker compose $(COMPOSE_ENV_FILES) --env-file envs/dev --file $(BASE_COMPOSE) pull postgres

ensure_pydocker: ensure_uv
	@$(PYTHON) -c "import docker" 2>/dev/null || \
	$(PYTHON) -m uv pip install docker

ensure_ruff: ensure_uv
	@$(PYTHON) -m ruff --version > /dev/null || \
	$(PYTHON) -m uv pip install ruff

ensure_uv:
	@$(PYTHON) -m uv --version > /dev/null || \
	$(PYTHON) -m pip install uv || \
	uv pip install --python "$(PYTHON)" uv

lint: ruff_lint prettier_lint # @doc lint (and auto-format) python, html and js

# prettier_lint rewrites files; prettier_check is the read-only variant CI runs (lint.yml).
prettier_check: # @doc fail if any html/js file is not prettier-formatted
	npx prettier --check $(html_files) $(js_files)

prettier_lint: /tmp/prettier.stamp
	true

/tmp/prettier.stamp: $(html_files) $(js_files)
	npx prettier --write $(html_files) $(js_files)
	touch /tmp/prettier.stamp

ruff_fix: ensure_ruff
	find $(PYTHON_DIRS) -name "*.py" | xargs $(PYTHON) -m ruff check --fix --unsafe-fixes >/dev/null 2>/dev/null || true
	find $(PYTHON_DIRS) -name "*.py" | xargs $(PYTHON) -m ruff format

ruff_lint: ruff_fix
	find $(PYTHON_DIRS) -name "*.py" | xargs $(PYTHON) -m ruff check --fix --unsafe-fixes

check_env: ensure_pydocker
	true

dockerclean:
	docker ps --all --format '{{.ID}}' | xargs $(MAYBENORUN) docker stop
	docker ps --all --format '{{.ID}}' | xargs $(MAYBENORUN) docker rm --force
	docker images --format '{{.ID}}' | xargs $(MAYBENORUN) docker rmi --force

dbconn-%: psql-dotfiles | .env .env.generated # @doc open psql against an environment, e.g. make dbconn-blue
	cd $(GIT_ROOT) && docker compose --project-name sylvan_$* $(COMPOSE_ENV_FILES) --env-file envs/$* $(call compose_files,$*) \
	  exec -e PSQLRC=/var/lib/postgresql/.psqlrc -e PSQL_HISTORY=/var/lib/postgresql/.psql_history \
	  postgres psql -U $(XPGUSER) -d $(XPGDATABASE) --host=localhost

reset-%: | .env .env.generated # @doc destroy an environment including its database volume
	docker compose --project-name sylvan_$* $(COMPOSE_ENV_FILES) --env-file envs/$* $(call compose_files,$*) down --volumes --remove-orphans
	rm -rvf data/api/$* data/postgres/$*

reset: $(addprefix reset-,$(ENVS)) # @doc destroy every environment including databases

install_deps:
	$(PYTHON) -m uv pip install -r requirements/base.txt

install_test_deps:
	$(PYTHON) -m uv pip install -r requirements/test.txt -r requirements/base.txt

js-deps: # @doc update JavaScript dependencies and apply safe security fixes
	npm update
	npm audit fix || true

engine: $(ENGINE_SO) # @doc build the Rust card engine extension if its sources changed

$(ENGINE_SO): $(engine_sources)
	@maturin --version > /dev/null 2>&1 || $(PYTHON) -m uv pip install maturin
	cd card_engine && PATH="$$HOME/.cargo/bin:$$PATH" maturin develop --release

test tests: install_test_deps engine
	$(PYTHON) -m pytest -vvv --capture=no --durations=10

test-integration: engine
	$(PYTHON) -m pytest api/tests/test_integration_testcontainers.py -vvv --exitfirst

test-unit: engine
	$(PYTHON) -m pytest -vvv --exitfirst --ignore=api/tests/test_integration_testcontainers.py

coverage: # @doc generate HTML coverage report
	$(PYTHON) -m pytest --cov=. --cov-report=html --cov-report=term-missing --durations=10 -vvv

test-profiling:
	$(PYTHON) -m pytest --profile-svg --durations=10 -vvv -k TestImportCardByName

font-dependencies:
	echo "Installing font subsetting dependencies..."
	$(PYTHON) -m uv pip install -r requirements/fonts.txt

fonts: mana_font beleren_font mplantin_font

mana_font: font-dependencies # @doc subset and optimize the Mana font for web delivery
	$(PYTHON) scripts/subset_mana_font.py \
		--output-dir data/fonts/mana \
		--cdn-url https://d1hot9ps2xugbc.cloudfront.net/cdn/fonts/mana \
		--s3-bucket $(S3_BUCKET) \
		--s3-prefix cdn/fonts/mana

beleren_font: font-dependencies # @doc subset and optimize the Beleren font for web delivery
	$(PYTHON) scripts/subset_beleren_font.py \
		--output-dir data/fonts/beleren \
		--cdn-url https://d1hot9ps2xugbc.cloudfront.net/cdn/fonts/beleren \
		--s3-bucket $(S3_BUCKET) \
		--s3-prefix cdn/fonts/beleren

mplantin_font: font-dependencies # @doc subset and optimize the MPlantin font for web delivery
	$(PYTHON) scripts/subset_mplantin_font.py \
		--input-font fonts/mplantin.otf \
		--output-dir data/fonts/mplantin \
		--cdn-url https://d1hot9ps2xugbc.cloudfront.net/cdn/fonts/mplantin \
		--s3-bucket $(S3_BUCKET) \
		--s3-prefix cdn/fonts/mplantin

compare-minification: # @doc compare file sizes: uncompressed, compressed, minified, and minified+compressed
	@echo "Installing minifier dependencies..."
	@npm install --no-save cssnano postcss postcss-cli terser > /dev/null 2>&1 || true
	@$(PYTHON) scripts/compare_minification.py

api/static/app.min.js: api/static/app.js # @doc minify app.js (used in both dev and prod)
	@echo "Minifying $^..."
	@npm install --no-save terser > /dev/null 2>&1 || true
	@npx terser api/static/app.js --compress 'pure_funcs=["console.debug","console.log"]' --mangle --output $@
	@echo "Created $@"
