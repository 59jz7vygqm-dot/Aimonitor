#!/bin/sh
# aimon-agent auf einem Server installieren.
#
#   curl -fsSL https://<aimon>/static/install.sh | \
#       AIMON_URL=http://aimon.lan:8420 AIMON_TOKEN=aimon_... sh
#
# oder lokal:
#   sudo AIMON_URL=... AIMON_TOKEN=... ./install.sh
#
# Nimmt eine vorhandene Binary aus dem Arbeitsverzeichnis, wenn vorhanden,
# sonst lädt es sie vom aimon-Server.

set -eu

BIN_DIR="${BIN_DIR:-/usr/local/bin}"
CONF_DIR="${CONF_DIR:-/etc/aimon}"
SERVICE_DIR="${SERVICE_DIR:-/etc/systemd/system}"
NODE_NAME="${AIMON_NODE_NAME:-$(hostname -s 2>/dev/null || hostname)}"
INSTALL_TAP="${INSTALL_TAP:-0}"

die() { echo "FEHLER: $*" >&2; exit 1; }
info() { echo "==> $*"; }

[ "$(id -u)" = "0" ] || die "bitte als root ausführen (sudo)."
[ -n "${AIMON_URL:-}" ] || die "AIMON_URL nicht gesetzt."
[ -n "${AIMON_TOKEN:-}" ] || die "AIMON_TOKEN nicht gesetzt. Token anlegen mit:
  curl -s -X POST \"\$AIMON_URL/api/v1/tokens\" \\
    -H \"Authorization: Bearer \$BOOTSTRAP_TOKEN\" \\
    -H 'Content-Type: application/json' \\
    -d '{\"name\":\"agent-$NODE_NAME\",\"scopes\":[\"ingest\"]}'"

command -v systemctl >/dev/null 2>&1 || die "kein systemd gefunden."

ARCH="$(uname -m)"
case "$ARCH" in
    x86_64|amd64) GOARCH=amd64 ;;
    aarch64|arm64) GOARCH=arm64 ;;
    *) die "nicht unterstützte Architektur: $ARCH" ;;
esac

# ------------------------------------------------------------------ Binary
if [ -x "./aimon-agent" ]; then
    info "Benutze ./aimon-agent aus dem Arbeitsverzeichnis."
    SRC="./aimon-agent"
else
    URL="${AIMON_URL%/}/static/dist/aimon-agent-linux-$GOARCH"
    info "Lade $URL"
    TMP="$(mktemp)"
    if command -v curl >/dev/null 2>&1; then
        curl -fsSL "$URL" -o "$TMP" || die "Download fehlgeschlagen. Binary mit
  'make agent' bauen und neben dieses Skript legen."
    elif command -v wget >/dev/null 2>&1; then
        wget -qO "$TMP" "$URL" || die "Download fehlgeschlagen."
    else
        die "weder curl noch wget vorhanden."
    fi
    SRC="$TMP"
fi

install -m 0755 "$SRC" "$BIN_DIR/aimon-agent"
info "Binary nach $BIN_DIR/aimon-agent installiert."

# ------------------------------------------------------------- Konfiguration
mkdir -p "$CONF_DIR"
if [ -f "$CONF_DIR/agent.yaml" ]; then
    # Eine bestehende Konfiguration nicht überschreiben - aber Token und
    # Server-Adresse nachziehen. Sonst wird ein frisch übergebenes Token
    # stillschweigend verworfen und die Maschine läuft weiter mit dem alten:
    # man glaubt, rotiert zu haben, und hat es nicht.
    CHANGED=""
    # Wert hinter dem Doppelpunkt holen, Anführungszeichen und Leerraum weg.
    yaml_value() {
        sed -n "s/^[[:space:]]*$1:[[:space:]]*//p" "$CONF_DIR/agent.yaml" \
            | head -1 \
            | sed -e 's/^["'"'"']//' -e 's/["'"'"']$//' -e 's/[[:space:]]*$//'
    }
    OLD_TOKEN="$(yaml_value token)"
    OLD_URL="$(yaml_value server_url)"

    if [ -n "$OLD_TOKEN" ] && [ "$OLD_TOKEN" != "$AIMON_TOKEN" ]; then
        cp "$CONF_DIR/agent.yaml" "$CONF_DIR/agent.yaml.bak"
        sed -i "s|^[[:space:]]*token:.*|token: $AIMON_TOKEN|" "$CONF_DIR/agent.yaml"
        CHANGED="Token"
    fi
    if [ -n "$OLD_URL" ] && [ "$OLD_URL" != "$AIMON_URL" ]; then
        [ -f "$CONF_DIR/agent.yaml.bak" ] || \
            cp "$CONF_DIR/agent.yaml" "$CONF_DIR/agent.yaml.bak"
        sed -i "s|^[[:space:]]*server_url:.*|server_url: $AIMON_URL|" \
            "$CONF_DIR/agent.yaml"
        CHANGED="${CHANGED:+$CHANGED und }Server-Adresse"
    fi

    if [ -n "$CHANGED" ]; then
        info "$CHANGED in $CONF_DIR/agent.yaml aktualisiert."
        info "Vorherige Fassung: $CONF_DIR/agent.yaml.bak"
    else
        info "Bestehende $CONF_DIR/agent.yaml bleibt unverändert (Token und"
        info "Server-Adresse stimmen bereits überein)."
    fi
else
    cat > "$CONF_DIR/agent.yaml" <<EOF
# Von install.sh erzeugt am $(date -Is)
server_url: $AIMON_URL
token: $AIMON_TOKEN
node_name: $NODE_NAME
interval: 10s
probe_interval: 30s
listen: 127.0.0.1:9100
gpu: true
host: true
discovery: true

labels:
${AIMON_LABELS:+$(echo "$AIMON_LABELS" | tr ',' '\n' | sed 's/^/  /; s/=/: /')}
  installed_by: install.sh

# Dienste auf nicht-Standardports hier eintragen:
# targets:
#   - kind: vllm
#     url: http://127.0.0.1:8000
EOF
    chmod 0600 "$CONF_DIR/agent.yaml"
    info "$CONF_DIR/agent.yaml angelegt (enthält das Token, Modus 0600)."
fi

# --------------------------------------------------------------- Selbsttest
info "Prüfe Verbindung und Collectors …"
CHECK_OUT="$(mktemp)"
if ! "$BIN_DIR/aimon-agent" check -config "$CONF_DIR/agent.yaml" 2>&1 \
        | tee "$CHECK_OUT"; then
    rm -f "$CHECK_OUT"
    die "Selbsttest fehlgeschlagen – siehe Ausgabe oben. Der Dienst wurde NICHT gestartet."
fi

# Ein Token, das nach einer anderen Maschine benannt ist, ist fast immer ein
# Versehen beim Kopieren - und es macht das Widerrufen einzelner Maschinen
# wirkungslos.
TOKEN_NAME="$(sed -n 's/.*OK: Token "\([^"]*\)".*/\1/p' "$CHECK_OUT" | head -1)"
rm -f "$CHECK_OUT"
case "$TOKEN_NAME" in
    "")  ;;
    *"$NODE_NAME"*) ;;
    *)
        echo ""
        echo "ACHTUNG: Das Token heißt \"$TOKEN_NAME\", diese Maschine heißt"
        echo "         \"$NODE_NAME\". Vermutlich wurde das Token einer anderen"
        echo "         Maschine kopiert. Das funktioniert, macht aber das"
        echo "         Widerrufen einzelner Maschinen unmöglich."
        echo "         Eigenes Token anlegen:"
        echo "           make token NAME=agent-$NODE_NAME"
        echo "         und die Installation mit dem neuen Token wiederholen."
        echo ""
        ;;
esac

# ----------------------------------------------------------------- systemd
cat > "$SERVICE_DIR/aimon-agent.service" <<'EOF'
[Unit]
Description=aimon-agent (Metrik-Sammler für aimonitor)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/local/bin/aimon-agent -config /etc/aimon/agent.yaml
Restart=always
RestartSec=10s
Nice=10
IOSchedulingClass=idle
CPUQuota=25%
MemoryMax=256M
User=root
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictSUIDSGID=true
RestrictRealtime=true
LockPersonality=true
ReadOnlyPaths=/etc/aimon
StandardOutput=journal
StandardError=journal
SyslogIdentifier=aimon-agent

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable aimon-agent.service >/dev/null 2>&1 || true
# `enable --now` startet einen bereits laufenden Dienst NICHT neu. Beim
# zweiten Lauf liefe sonst weiter die alte Binary, obwohl die neue schon auf
# der Platte liegt - und man sucht den Fehler an der falschen Stelle.
systemctl restart aimon-agent.service
sleep 1
if systemctl is-active --quiet aimon-agent.service; then
    RUNNING_VERSION="$("$BIN_DIR/aimon-agent" version 2>/dev/null || echo "?")"
    info "aimon-agent läuft (Version $RUNNING_VERSION, neu gestartet)."
else
    echo ""
    echo "FEHLER: aimon-agent startet nicht. Ausgabe:" >&2
    journalctl -u aimon-agent -n 20 --no-pager >&2 || true
    exit 1
fi

if [ "$INSTALL_TAP" = "1" ]; then
    [ -n "${TAP_UPSTREAM:-}" ] || die "INSTALL_TAP=1 braucht TAP_UPSTREAM."
    TAP_LISTEN="${TAP_LISTEN:-0.0.0.0:9500}"
    {
        echo ""
        echo "tap_listen: $TAP_LISTEN"
        echo "tap_upstream: $TAP_UPSTREAM"
        echo "tap_kind: ${TAP_KIND:-vllm}"
    } >> "$CONF_DIR/agent.yaml"
    cat > "$SERVICE_DIR/aimon-tap.service" <<'EOF'
[Unit]
Description=aimon-tap (Messproxy im Inference-Pfad)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/local/bin/aimon-agent tap -config /etc/aimon/agent.yaml
Restart=always
RestartSec=5s
LimitNOFILE=65536
DynamicUser=yes
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ReadOnlyPaths=/etc/aimon
StandardOutput=journal
StandardError=journal
SyslogIdentifier=aimon-tap

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    systemctl enable aimon-tap.service >/dev/null 2>&1 || true
    systemctl restart aimon-tap.service
    info "aimon-tap läuft auf $TAP_LISTEN -> $TAP_UPSTREAM."
    info "Jetzt noch OpenWebUI auf http://<dieser-host>:${TAP_LISTEN##*:} umstellen,"
    info "sonst fließt kein Verkehr durch den Tap und es gibt keine TTFT-Daten."
fi

echo ""
info "Fertig. Nützliche Befehle:"
echo "  systemctl status aimon-agent"
echo "  journalctl -u aimon-agent -f"
echo "  curl -s localhost:9100/healthz"
echo "  aimon-agent check            # Konfiguration und Dienste prüfen"
echo "  aimon-agent discover         # gefundene Dienste als YAML ausgeben"
