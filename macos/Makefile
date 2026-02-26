# Harbor Clerk — macOS native app build system
# Usage: make all        (build everything)
#        make deps       (build PostgreSQL, Tesseract, Tika/JRE)
#        make venv       (build Python + venv)
#        make model      (download embedding model)
#        make frontend   (build React SPA)
#        make dev        (frontend + Xcode debug build)
#        make test       (run Xcode tests)
#        make apps       (build Xcode projects + assemble bundles)
#        make sign       (codesign + notarize)
#        make clean      (remove build artifacts)

SCRIPTS := scripts
BUILD   := build

.PHONY: all deps postgres tika tools venv model llama frontend dev test apps sign clean

all: deps venv model llama apps

# ── Dependencies ──

deps: postgres tika tools

postgres:
	@echo "==> Building PostgreSQL + pgvector"
	DEST_DIR=$(BUILD)/postgres BUILD_DIR=$(BUILD)/pg-build bash $(SCRIPTS)/build-postgres.sh

tika:
	@echo "==> Downloading JRE + Tika"
	DEST_DIR=$(BUILD) bash $(SCRIPTS)/build-tika.sh

tools:
	@echo "==> Extracting Tesseract"
	DEST_DIR=$(BUILD) bash $(SCRIPTS)/build-deps.sh

# ── Python ──

venv:
	@echo "==> Building Python + venv"
	DEST_DIR=$(BUILD) PROJECT_ROOT=$(shell cd .. && pwd) bash $(SCRIPTS)/build-venv.sh
	git -C $(shell cd .. && pwd) rev-parse --short HEAD > $(BUILD)/venv/build-hash.txt

# ── Model ──
# DEST_DIR must match the path in EmbedderService.swift ("model/<name>")

model:
	@echo "==> Downloading embedder model"
	DEST_DIR=$(BUILD)/model/all-MiniLM-L6-v2 VENV_DIR=$(BUILD)/venv bash $(SCRIPTS)/download-model.sh

# ── LLM server ──

llama:
	@echo "==> Building llama-server"
	DEST_DIR=$(BUILD)/llama BUILD_DIR=$(BUILD)/llama-build bash $(SCRIPTS)/build-llama.sh

# ── Frontend ──

frontend:
	@echo "==> Building frontend"
	cd ../frontend && npm ci && npm run build

# ── Dev (debug build with frontend, no deps/venv/model needed) ──

dev: frontend
	@echo "==> Building Xcode project (Debug)"
	cd HarborClerkServer && xcodebuild -project HarborClerkServer.xcodeproj \
		-scheme HarborClerkServer -configuration Debug build

# ── Test ──

test:
	cd HarborClerkServer && xcodebuild -project HarborClerkServer.xcodeproj \
		-scheme HarborClerkServer -configuration Debug test

# ── Apps ──

apps: frontend
	@echo "==> Building and packaging apps"
	BUILD_DIR=$(BUILD) bash $(SCRIPTS)/package.sh

# ── Signing ──

sign:
	@echo "==> Signing and notarizing"
	BUILD_DIR=$(BUILD) bash $(SCRIPTS)/notarize.sh

# ── Cleanup ──

clean:
	rm -rf $(BUILD) 2>/dev/null; rm -rf $(BUILD) 2>/dev/null; true
	@echo "==> Build artifacts cleaned"
