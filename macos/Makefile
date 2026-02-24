# Harbor Clerk — macOS native app build system
# Usage: make all        (build everything)
#        make deps       (build PostgreSQL, Tesseract, Tika/JRE)
#        make venv       (build Python + venv)
#        make model      (download sentence-transformers model)
#        make apps       (build Xcode projects + assemble bundles)
#        make sign       (codesign + notarize)
#        make clean      (remove build artifacts)

SCRIPTS := scripts
BUILD   := build

.PHONY: all deps postgres tika tools venv model llama apps sign clean

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

# ── Model ──

model:
	@echo "==> Downloading embedder model"
	DEST_DIR=$(BUILD)/model/all-MiniLM-L6-v2 VENV_DIR=$(BUILD)/venv bash $(SCRIPTS)/download-model.sh

# ── LLM server ──

llama:
	@echo "==> Building llama-server"
	DEST_DIR=$(BUILD)/llama BUILD_DIR=$(BUILD)/llama-build bash $(SCRIPTS)/build-llama.sh

# ── Apps ──

apps:
	@echo "==> Building and packaging apps"
	BUILD_DIR=$(BUILD) bash $(SCRIPTS)/package.sh

# ── Signing ──

sign:
	@echo "==> Signing and notarizing"
	BUILD_DIR=$(BUILD) bash $(SCRIPTS)/notarize.sh

# ── Cleanup ──

clean:
	rm -rf $(BUILD)
	@echo "==> Build artifacts cleaned"
