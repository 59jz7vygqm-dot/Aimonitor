package collector

import (
	"context"
	"fmt"
	"net"
	"net/http"
	"strings"
	"time"

	"github.com/aimonitor/agent/internal/config"
)

// Discovery findet lokal laufende Dienste, damit ein neuer Server ohne
// zentrale Konfiguration angebunden werden kann: Agent installieren, fertig.
//
// Ablauf: bekannte Ports auf localhost antesten, dann per Fingerabdruck
// bestätigen. Ein offener Port allein reicht nicht - 8000 kann alles sein.

type candidate struct {
	port int
	kind string
	path string
	// want muss im Antworttext vorkommen, damit der Fund zählt.
	want []string
}

var candidates = []candidate{
	{8000, "vllm", "/metrics", []string{"vllm:"}},
	{8001, "vllm", "/metrics", []string{"vllm:"}},
	{8080, "vllm", "/metrics", []string{"vllm:"}},
	{11434, "ollama", "/api/version", []string{"version"}},
	{8188, "comfyui", "/system_stats", []string{"comfyui_version", "devices"}},
	{3000, "openwebui", "/health", []string{"true", "status", "ok"}},
	{8080, "openwebui", "/health", []string{"true", "status"}},
	{8888, "searxng", "/healthz", []string{"OK", "ok"}},
	{8080, "searxng", "/healthz", []string{"OK", "ok"}},
}

// Discover probiert die Kandidatenports und gibt bestätigte Ziele zurück.
func Discover(ctx context.Context, nodeName string, timeout time.Duration) []config.Target {
	client := &http.Client{Timeout: timeout}
	seen := map[string]bool{}
	var out []config.Target

	for _, c := range candidates {
		addr := fmt.Sprintf("127.0.0.1:%d", c.port)
		conn, err := net.DialTimeout("tcp", addr, 500*time.Millisecond)
		if err != nil {
			continue
		}
		conn.Close()

		url := fmt.Sprintf("http://%s%s", addr, c.path)
		req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
		if err != nil {
			continue
		}
		req.Header.Set("User-Agent", "aimon-agent/discovery")
		resp, err := client.Do(req)
		if err != nil {
			continue
		}
		buf := make([]byte, 16384)
		n, _ := resp.Body.Read(buf)
		resp.Body.Close()
		if resp.StatusCode >= 400 {
			continue
		}
		body := string(buf[:n])

		match := len(c.want) == 0
		for _, w := range c.want {
			if strings.Contains(body, w) {
				match = true
				break
			}
		}
		if !match {
			continue
		}

		base := fmt.Sprintf("http://127.0.0.1:%d", c.port)
		key := config.DeriveKey(c.kind, base, nodeName)
		if seen[key] {
			continue
		}
		seen[key] = true
		out = append(out, config.Target{
			Kind:   c.kind,
			URL:    base,
			Key:    key,
			Probe:  c.path,
			Labels: map[string]string{"discovered": "true"},
		})
	}
	return out
}

// MergeTargets fügt gefundene Ziele zu den konfigurierten hinzu, ohne
// Konfiguriertes zu überschreiben - die Datei gewinnt immer.
func MergeTargets(configured, discovered []config.Target) []config.Target {
	have := map[string]bool{}
	for _, t := range configured {
		have[t.Key] = true
		have[strings.TrimRight(t.URL, "/")] = true
	}
	out := append([]config.Target(nil), configured...)
	for _, d := range discovered {
		if have[d.Key] || have[strings.TrimRight(d.URL, "/")] {
			continue
		}
		out = append(out, d)
	}
	return out
}
