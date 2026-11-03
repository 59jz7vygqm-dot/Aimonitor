// Package config lädt die Agent-Konfiguration.
//
// Format ist ein bewusst minimaler YAML-Dialekt (flache Keys + Listen von
// Objekten), damit der Agent ohne externe Abhängigkeit statisch bleibt.
// Umgebungsvariablen haben Vorrang vor der Datei.
package config

import (
	"fmt"
	"os"
	"strconv"
	"strings"
	"time"
)

type Target struct {
	Kind   string // vllm | ollama | comfyui | openwebui | searxng | ...
	Name   string // Anzeigename
	URL    string // Basis-URL
	Key    string // Service-Key; leer -> wird abgeleitet
	Labels map[string]string
	Probe  string   // Health-Pfad; leer -> kindspezifischer Default
	Models []string // nur Ollama: Modelle für TTFT-Probes
}

type Config struct {
	ServerURL string
	Token     string
	NodeName  string
	Hostname  string
	Labels    map[string]string

	Interval      time.Duration
	ProbeInterval time.Duration
	Timeout       time.Duration
	BufferBatches int
	ListenAddr    string // Debug-Endpunkt, leer = aus

	GPUEnabled       bool
	HostEnabled      bool
	DiscoveryEnabled bool
	OllamaProbe      bool

	// Für den SSH-Befehl, den das Dashboard anzeigt. Leer = automatisch
	// ermittelt bzw. weggelassen.
	SSHUser string
	SSHPort string
	// ReportAddresses: Adressen des Hosts mitschicken. Wer das nicht will
	// (etwa weil das Dashboard weitergegeben wird), schaltet es hier ab.
	ReportAddresses bool

	Targets []Target
	Peers   []string // zusätzliche Probe-Ziele (URL oder host:port)

	// Tap-Modus
	TapListen   string
	TapUpstream string
	TapService  string
	TapKind     string
}

func Default() Config {
	host, _ := os.Hostname()
	return Config{
		ServerURL:        "http://localhost:8420",
		NodeName:         host,
		Hostname:         host,
		Labels:           map[string]string{},
		Interval:         10 * time.Second,
		ProbeInterval:    30 * time.Second,
		Timeout:          5 * time.Second,
		BufferBatches:    120,
		GPUEnabled:       true,
		HostEnabled:      true,
		DiscoveryEnabled: true,
		OllamaProbe:      false,
		ReportAddresses:  true,
		TapListen:        "0.0.0.0:9500",
		TapKind:          "vllm",
	}
}

// Load liest die Datei (wenn vorhanden) und überlagert sie mit Umgebungsvariablen.
func Load(path string) (Config, error) {
	c := Default()
	if path != "" {
		if data, err := os.ReadFile(path); err == nil {
			if err := parse(string(data), &c); err != nil {
				return c, fmt.Errorf("%s: %w", path, err)
			}
		} else if !os.IsNotExist(err) {
			return c, fmt.Errorf("%s: %w", path, err)
		}
	}
	applyEnv(&c)
	if c.Token == "" {
		return c, fmt.Errorf("kein Token: AIMON_TOKEN setzen oder token: in der Konfiguration")
	}
	if c.NodeName == "" {
		return c, fmt.Errorf("kein node_name und kein Hostname ermittelbar")
	}
	for i := range c.Targets {
		t := &c.Targets[i]
		if t.Key == "" {
			t.Key = DeriveKey(t.Kind, t.URL, c.NodeName)
		}
	}
	return c, nil
}

// DeriveKey baut den Service-Key nach der Konvention <kind>@<host>:<port>.
func DeriveKey(kind, rawURL, fallbackHost string) string {
	hostport := rawURL
	if i := strings.Index(hostport, "://"); i >= 0 {
		hostport = hostport[i+3:]
	}
	if i := strings.IndexAny(hostport, "/?"); i >= 0 {
		hostport = hostport[:i]
	}
	host, port := hostport, ""
	if i := strings.LastIndex(hostport, ":"); i > 0 {
		host, port = hostport[:i], hostport[i+1:]
	}
	if host == "" || host == "localhost" || host == "127.0.0.1" || host == "0.0.0.0" {
		host = fallbackHost
	}
	if port == "" {
		return fmt.Sprintf("%s@%s", kind, host)
	}
	return fmt.Sprintf("%s@%s:%s", kind, host, port)
}

func applyEnv(c *Config) {
	envStr("AIMON_URL", &c.ServerURL)
	envStr("AIMON_TOKEN", &c.Token)
	envStr("AIMON_NODE_NAME", &c.NodeName)
	envStr("AIMON_LISTEN", &c.ListenAddr)
	envStr("AIMON_TAP_LISTEN", &c.TapListen)
	envStr("AIMON_TAP_UPSTREAM", &c.TapUpstream)
	envStr("AIMON_TAP_SERVICE", &c.TapService)
	envStr("AIMON_TAP_KIND", &c.TapKind)
	envDur("AIMON_INTERVAL", &c.Interval)
	envDur("AIMON_PROBE_INTERVAL", &c.ProbeInterval)
	envDur("AIMON_TIMEOUT", &c.Timeout)
	envBool("AIMON_GPU", &c.GPUEnabled)
	envBool("AIMON_HOST", &c.HostEnabled)
	envBool("AIMON_DISCOVERY", &c.DiscoveryEnabled)
	envBool("AIMON_OLLAMA_PROBE", &c.OllamaProbe)
	envBool("AIMON_REPORT_ADDRESSES", &c.ReportAddresses)
	envStr("AIMON_SSH_USER", &c.SSHUser)
	envStr("AIMON_SSH_PORT", &c.SSHPort)
	if v := os.Getenv("AIMON_LABELS"); v != "" {
		for _, kv := range strings.Split(v, ",") {
			if k, val, ok := strings.Cut(strings.TrimSpace(kv), "="); ok {
				c.Labels[k] = val
			}
		}
	}
	if v := os.Getenv("AIMON_PEERS"); v != "" {
		for _, p := range strings.Split(v, ",") {
			if p = strings.TrimSpace(p); p != "" {
				c.Peers = append(c.Peers, p)
			}
		}
	}
}

func envStr(k string, dst *string) {
	if v := os.Getenv(k); v != "" {
		*dst = v
	}
}

func envBool(k string, dst *bool) {
	if v := os.Getenv(k); v != "" {
		switch strings.ToLower(strings.TrimSpace(v)) {
		case "1", "true", "yes", "on":
			*dst = true
		case "0", "false", "no", "off":
			*dst = false
		}
	}
}

func envDur(k string, dst *time.Duration) {
	if v := os.Getenv(k); v != "" {
		if d, err := time.ParseDuration(v); err == nil {
			*dst = d
		} else if n, err := strconv.Atoi(v); err == nil {
			*dst = time.Duration(n) * time.Second
		}
	}
}
