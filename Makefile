.DEFAULT_GOAL := prepare

.PHONY: help
help: ## Show available make targets.
	@echo "Available make targets:"
	@awk 'BEGIN { FS = ":.*## " } /^[A-Za-z0-9_.-]+:.*## / { printf "  %-20s %s\n", $$1, $$2 }' $(MAKEFILE_LIST)

.PHONY: install-prek
install-prek: ## Install prek and repo git hooks.
	@echo "==> Installing prek"
	@uv tool install prek
	@echo "==> Installing git hooks with prek"
	@uv tool run prek install

.PHONY: prepare
prepare: download-deps install-prek ## Sync dependencies for all workspace packages and install prek hooks.
	@echo "==> Syncing dependencies for all workspace packages"
	@uv sync --frozen --all-extras --all-packages

.PHONY: prepare-build
prepare-build: download-deps ## Sync dependencies for releases without workspace sources.
	@echo "==> Syncing dependencies for release builds (no sources)"
	@uv sync --all-extras --all-packages --no-sources

# for kimo web development
.PHONY: web-back web-front
web-back: ## Start web backend with uvicorn (reload enabled).
	@LOG_LEVEL=DEBUG uv run uvicorn kimo_cli.web.app:create_app --factory --reload --port 5494
web-front: ## Start web frontend (vite dev server).
	@npm --prefix web run dev

# for kimo vis development
.PHONY: vis-back vis-front
vis-back: ## Start vis backend with uvicorn (reload enabled).
	@LOG_LEVEL=DEBUG uv run uvicorn kimo_cli.vis.app:create_app --factory --reload --port 5495
vis-front: ## Start vis frontend (vite dev server).
	@npm --prefix vis run dev

.PHONY: format format-kimo-cli format-kosong format-pykaos format-web
format: format-kimo-cli format-kosong format-pykaos format-web ## Auto-format all workspace packages.
format-kimo-cli: ## Auto-format Kimo Code CLI sources with ruff.
	@echo "==> Formatting Kimo Code CLI sources"
	@uv run ruff check --fix
	@uv run ruff format
format-kosong: ## Auto-format kosong sources with ruff.
	@echo "==> Formatting kosong sources"
	@uv run --project packages/kosong --directory packages/kosong ruff check --fix
	@uv run --project packages/kosong --directory packages/kosong ruff format
format-pykaos: ## Auto-format pykaos sources with ruff.
	@echo "==> Formatting pykaos sources"
	@uv run --project packages/kaos --directory packages/kaos ruff check --fix
	@uv run --project packages/kaos --directory packages/kaos ruff format

.PHONY: gen-changelog gen-docs
gen-changelog: ## Generate changelog with Kimo Code CLI.
	@echo "==> Generating changelog"
	@uv run kimo --yolo --prompt /skill:gen-changelog
gen-docs: ## Generate user docs with Kimo Code CLI.
	@echo "==> Generating user docs"
	@uv run kimo --yolo --prompt /skill:gen-docs

include src/kimo_cli/deps/Makefile
