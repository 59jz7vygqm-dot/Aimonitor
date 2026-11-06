# aimonitor
SHELL := /bin/bash
VERSION ?= 0.1.0
# Commit-Kennung, damit man einem laufenden Agent oder Server ansieht, welcher
# Stand darin steckt. "-dirty" heißt: nicht committete Änderungen.
BUILD  ?= $(shell git describe --always --dirty --tags 2>/dev/null || echo unbekannt)
GOFLAGS := -trimpath -ldflags "-s -w -X main.version=$(VERSION)+$(BUILD)"

.PHONY: help
help:
	@echo "aimonitor $(VERSION)"
	@echo ""
	@echo "  make agent          Agent-Binary für diese Plattform bauen"
	@echo "  make agent-all      Binaries für linux/amd64 und linux/arm64 (braucht Go)"
	@echo "  make agent-docker   dieselben Binaries, gebaut im Container (ohne Go)"
	@echo "  make test           Alle Tests (Go + Python)"
	@echo "  make test-go        Go-Tests mit Race-Detector"
	@echo "  make test-py        Python-Tests (braucht eine Test-DB)"
	@echo "  make lint           go vet + Format-Prüfung"
	@echo "  make up             Zentralen Stack starten (docker compose)"
	@echo "  make down           Stack stoppen"
	@echo "  make logs           Logs des Servers"
	@echo "  make dev            Server lokal ohne Docker (braucht PostgreSQL)"
	@echo "  make simulate       Testdaten in einen laufenden Server schieben"
	@echo "  make fake-stack     Fake-vLLM/Ollama/ComfyUI auf localhost"
	@echo "  make token NAME=x   Ingest-Token anlegen"
	@echo "  make clean          Build-Verzeichnis web/dist leeren"
	@echo "  make version        Welcher Stand liegt hier, welcher läuft"

# ------------------------------------------------------------------- Agent
.PHONY: agent
agent:
	@mkdir -p web/dist
	cd agent && CGO_ENABLED=0 go build $(GOFLAGS) -o ../web/dist/aimon-agent .
	@echo "-> web/dist/aimon-agent"

# Baut die Binaries in einem Container - ohne Go auf dem Host.
.PHONY: agent-docker
agent-docker:
	@mkdir -p web/dist
	docker run --rm -v "$(CURDIR)/agent:/src:ro" -v "$(CURDIR)/web/dist:/out" \
		-w /src -e CGO_ENABLED=0 -e GOFLAGS=-buildvcs=false \
		-e GOCACHE=/tmp/gocache -e GOPATH=/tmp/gopath \
		golang:1.22-alpine sh -c '\
		  go build -trimpath -ldflags "-s -w -X main.version=$(VERSION)+$(BUILD)" \
		    -o /out/aimon-agent-linux-amd64 . && \
		  GOARCH=arm64 go build -trimpath \
		    -ldflags "-s -w -X main.version=$(VERSION)+$(BUILD)" \
		    -o /out/aimon-agent-linux-arm64 .'
	cp deploy/agent/install.sh web/dist/install.sh
	@cd web/dist && sha256sum aimon-agent-linux-* install.sh > SHA256SUMS
	@echo ""
	@echo "Binaries in web/dist/. Der Server liefert sie unter /static/dist/ aus,"
	@echo "sobald der Container neu gestartet wurde:"
	@echo "  cd deploy && docker compose restart server"
	@ls -lh web/dist/

.PHONY: agent-all
agent-all:
	@mkdir -p web/dist
	cd agent && CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build $(GOFLAGS) \
		-o ../web/dist/aimon-agent-linux-amd64 .
	cd agent && CGO_ENABLED=0 GOOS=linux GOARCH=arm64 go build $(GOFLAGS) \
		-o ../web/dist/aimon-agent-linux-arm64 .
	cp deploy/agent/install.sh web/dist/install.sh
	@cd web/dist && sha256sum aimon-agent-linux-* install.sh > SHA256SUMS
	@echo ""
	@echo "Binaries in web/dist/ - der Server liefert sie unter /static/dist/ aus,"
	@echo "sodass install.sh sie herunterladen kann."
	@ls -lh web/dist/

# ------------------------------------------------------------------- Tests
.PHONY: test
test: test-go test-py

.PHONY: test-go
test-go:
	cd agent && go test ./... -race -count=1

.PHONY: lint
lint:
	cd agent && go vet ./...
	@cd agent && test -z "$$(gofmt -l .)" || \
		{ echo "nicht formatiert:"; gofmt -l .; exit 1; }

.PHONY: test-py
test-py:
	cd server && python -m pytest -q

# ------------------------------------------------------------------ Betrieb
AIMON_URL ?= http://localhost:8420

.PHONY: up
up:
	cd deploy && AIMON_BUILD=$(BUILD) docker compose up -d --build --force-recreate
	@echo ""
	@printf 'Warte auf den Server '
	@for i in $$(seq 1 45); do \
		if curl -fsS --max-time 2 "$(AIMON_URL)/api/v1/health" >/dev/null 2>&1; then \
			echo " bereit."; break; \
		fi; \
		printf '.'; sleep 2; \
		if [ $$i -eq 45 ]; then \
			echo ""; \
			echo "Der Server antwortet nach 90 s nicht. Logs ansehen:"; \
			echo "  cd deploy && docker compose logs --tail=50 server"; \
		fi; \
	done
	@echo ""
	@$(MAKE) --no-print-directory version

# Vergleicht den Stand im Arbeitsverzeichnis mit dem, was tatsächlich läuft.
# Gedacht für die Frage "warum sehe ich meine Änderung nicht".
#
# Bewusst ohne python und ohne Pipe-Akrobatik: dieses Ziel wird genau dann
# aufgerufen, wenn etwas nicht stimmt - dann darf es nicht selbst mit einem
# Traceback aussteigen.
.PHONY: version
version:
	@echo "Arbeitsverzeichnis: $(VERSION)+$(BUILD)"
	@body="$$(curl -fsS --max-time 5 "$(AIMON_URL)/api/v1/health" 2>/dev/null || true)"; \
	if [ -z "$$body" ]; then \
		echo "Server:             nicht erreichbar ($(AIMON_URL))"; \
		echo ""; \
		echo "Läuft der Container? cd deploy && docker compose ps"; \
		exit 0; \
	fi; \
	ver="$$(printf '%s' "$$body" \
		| sed -n 's/.*"version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')"; \
	bld="$$(printf '%s' "$$body" \
		| sed -n 's/.*"build"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')"; \
	[ -n "$$ver" ] || ver="?"; \
	[ -n "$$bld" ] || bld="unbekannt"; \
	echo "Server:             $$ver+$$bld"; \
	echo ""; \
	if [ "$$ver+$$bld" = "$(VERSION)+$(BUILD)" ]; then \
		echo "Stimmt überein."; \
	elif [ "$$bld" = "unbekannt" ]; then \
		echo "Der Server kennt seinen Stand nicht – mit 'docker compose up --build'"; \
		echo "statt 'make up' gebaut. Noch einmal mit:"; \
		echo "  make up"; \
	else \
		echo "Es läuft ein anderer Stand als hier im Verzeichnis:"; \
		echo "  git pull && make up"; \
	fi

.PHONY: down
down:
	cd deploy && docker compose down

.PHONY: logs
logs:
	cd deploy && docker compose logs -f server

.PHONY: dev
dev:
	cd server && python -m uvicorn aimon.main:app --reload --host 0.0.0.0 --port 8420

.PHONY: simulate
simulate:
	python3 tools/simulate.py --url $${AIMON_URL:-http://localhost:8420} \
		--token $${AIMON_TOKEN:?AIMON_TOKEN setzen} --minutes $${MINUTES:-60} \
		--step $${STEP:-15} $${PROBLEMS:+--problems}

.PHONY: fake-stack
fake-stack:
	python3 tools/fake_stack.py --slow $${SLOW:-0.2}

.PHONY: token
token:
	@test -n "$(NAME)" || { echo "NAME= angeben"; exit 1; }
	@curl -fsS -X POST "$${AIMON_URL:-http://localhost:8420}/api/v1/tokens" \
		-H "Authorization: Bearer $${AIMON_BOOTSTRAP_TOKEN:?AIMON_BOOTSTRAP_TOKEN setzen}" \
		-H 'Content-Type: application/json' \
		-d '{"name":"$(NAME)","scopes":["ingest"]}' | python3 -m json.tool

.PHONY: clean
clean:
	rm -rf ./web/dist
	cd agent && go clean -testcache 2>/dev/null || true
