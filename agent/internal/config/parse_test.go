package config

import (
	"os"
	"path/filepath"
	"testing"
	"time"
)

const sample = `
# aimon-agent
server_url: http://aimon.lan:8420
token: aimon_secret
node_name: gpu-vm-01
interval: 15s
probe_interval: 45s
gpu: true
discovery: false

labels:
  site: rack-a
  role: inference

peers:
  - http://proxy-host-01:3000/health
  - proxy-host-01:8888

targets:
  - kind: vllm
    url: http://127.0.0.1:8000
    name: vLLM Qwen
    labels:
      model: Qwen3-32B-AWQ
      tp: "2"
  - kind: ollama
    url: http://127.0.0.1:11434
    models: gemma3:27b, nomic-embed-text
  - kind: comfyui
    url: http://127.0.0.1:8188
`

func TestParse(t *testing.T) {
	c := Default()
	c.Labels = map[string]string{}
	if err := parse(sample, &c); err != nil {
		t.Fatalf("parse: %v", err)
	}
	if c.ServerURL != "http://aimon.lan:8420" {
		t.Errorf("server_url = %q", c.ServerURL)
	}
	if c.Token != "aimon_secret" {
		t.Errorf("token = %q", c.Token)
	}
	if c.Interval != 15*time.Second {
		t.Errorf("interval = %v", c.Interval)
	}
	if c.ProbeInterval != 45*time.Second {
		t.Errorf("probe_interval = %v", c.ProbeInterval)
	}
	if !c.GPUEnabled {
		t.Error("gpu sollte true sein")
	}
	if c.DiscoveryEnabled {
		t.Error("discovery sollte false sein")
	}
	if c.Labels["site"] != "rack-a" || c.Labels["role"] != "inference" {
		t.Errorf("labels = %v", c.Labels)
	}
	if len(c.Peers) != 2 || c.Peers[1] != "proxy-host-01:8888" {
		t.Errorf("peers = %v", c.Peers)
	}
	if len(c.Targets) != 3 {
		t.Fatalf("targets = %d, erwartet 3: %+v", len(c.Targets), c.Targets)
	}
	v := c.Targets[0]
	if v.Kind != "vllm" || v.URL != "http://127.0.0.1:8000" || v.Name != "vLLM Qwen" {
		t.Errorf("target[0] = %+v", v)
	}
	if v.Labels["model"] != "Qwen3-32B-AWQ" || v.Labels["tp"] != "2" {
		t.Errorf("target[0].labels = %v", v.Labels)
	}
	o := c.Targets[1]
	if len(o.Models) != 2 || o.Models[0] != "gemma3:27b" {
		t.Errorf("target[1].models = %v", o.Models)
	}
	if c.Targets[2].Kind != "comfyui" {
		t.Errorf("target[2] = %+v", c.Targets[2])
	}
}

func TestDeriveKey(t *testing.T) {
	cases := []struct{ kind, url, host, want string }{
		{"vllm", "http://127.0.0.1:8000", "gpu-vm-01", "vllm@gpu-vm-01:8000"},
		{"vllm", "http://10.0.0.5:8000/v1", "gpu-vm-01", "vllm@10.0.0.5:8000"},
		{"ollama", "http://localhost:11434", "gpu-vm-02", "ollama@gpu-vm-02:11434"},
		{"comfyui", "comfy.lan:8188", "x", "comfyui@comfy.lan:8188"},
		{"searxng", "http://search.lan", "x", "searxng@search.lan"},
	}
	for _, c := range cases {
		if got := DeriveKey(c.kind, c.url, c.host); got != c.want {
			t.Errorf("DeriveKey(%q,%q) = %q, erwartet %q", c.kind, c.url, got, c.want)
		}
	}
}

func TestLoadEnvOverridesFile(t *testing.T) {
	dir := t.TempDir()
	p := filepath.Join(dir, "agent.yaml")
	if err := os.WriteFile(p, []byte("server_url: http://file:8420\ntoken: from-file\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	t.Setenv("AIMON_URL", "http://env:9999")
	c, err := Load(p)
	if err != nil {
		t.Fatalf("Load: %v", err)
	}
	if c.ServerURL != "http://env:9999" {
		t.Errorf("Umgebung muss Datei überschreiben, got %q", c.ServerURL)
	}
	if c.Token != "from-file" {
		t.Errorf("token = %q", c.Token)
	}
}

func TestLoadRequiresToken(t *testing.T) {
	if _, err := Load(filepath.Join(t.TempDir(), "fehlt.yaml")); err == nil {
		t.Error("ohne Token muss Load fehlschlagen")
	}
}

func TestUnknownKeyIsNotFatal(t *testing.T) {
	c := Default()
	if err := parse("token: x\nzukunfts_option: 42\n", &c); err != nil {
		t.Errorf("unbekannter Key darf nicht fehlschlagen: %v", err)
	}
}
