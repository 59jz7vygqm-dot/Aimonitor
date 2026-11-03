package config

import (
	"fmt"
	"strconv"
	"strings"
	"time"
)

// parse liest einen bewusst kleinen YAML-Dialekt:
//
//	server_url: http://aimon:8420
//	token: aimon_...
//	node_name: gpu-vm-01
//	interval: 10s
//	labels:
//	  site: rack-a
//	peers:
//	  - http://other:8000/health
//	targets:
//	  - kind: vllm
//	    url: http://127.0.0.1:8000
//	    labels:
//	      model: Qwen3-32B
//
// Kein Alias, kein Anchor, keine mehrzeiligen Strings. Dafür keine Abhängigkeit,
// und der Agent bleibt eine statische Binary ohne cgo.
func parse(text string, c *Config) error {
	lines := strings.Split(text, "\n")

	type ctx struct {
		section string // "", "labels", "peers", "targets"
		indent  int
	}
	cur := ctx{}
	var target *Target
	var targetSub string // "labels" innerhalb eines Targets

	flushTarget := func() {
		if target != nil {
			c.Targets = append(c.Targets, *target)
			target = nil
			targetSub = ""
		}
	}

	for ln, raw := range lines {
		line := strings.TrimRight(raw, " \t\r")
		if line == "" || strings.HasPrefix(strings.TrimSpace(line), "#") {
			continue
		}
		indent := len(line) - len(strings.TrimLeft(line, " \t"))
		body := strings.TrimSpace(line)

		// Listeneintrag
		if strings.HasPrefix(body, "- ") || body == "-" {
			item := strings.TrimSpace(strings.TrimPrefix(body, "-"))
			switch cur.section {
			case "peers":
				if item != "" {
					c.Peers = append(c.Peers, unquote(item))
				}
			case "targets":
				flushTarget()
				target = &Target{Labels: map[string]string{}}
				targetSub = ""
				if item != "" {
					if k, v, ok := strings.Cut(item, ":"); ok {
						if err := setTargetField(target, strings.TrimSpace(k),
							unquote(strings.TrimSpace(v))); err != nil {
							return fmt.Errorf("Zeile %d: %w", ln+1, err)
						}
					}
				}
			default:
				return fmt.Errorf("Zeile %d: Listeneintrag außerhalb einer Liste", ln+1)
			}
			continue
		}

		key, val, hasColon := strings.Cut(body, ":")
		if !hasColon {
			return fmt.Errorf("Zeile %d: erwartet 'key: wert', bekommen %q", ln+1, body)
		}
		key = strings.TrimSpace(key)
		val = unquote(strings.TrimSpace(val))

		// Verschachtelung innerhalb eines Targets
		if target != nil && indent > 0 {
			if val == "" && key == "labels" {
				targetSub = "labels"
				continue
			}
			if targetSub == "labels" && indent >= 4 {
				target.Labels[key] = val
				continue
			}
			if targetSub == "models" || (val == "" && key == "models") {
				targetSub = "models"
				if val != "" {
					target.Models = append(target.Models, val)
				}
				continue
			}
			targetSub = ""
			if err := setTargetField(target, key, val); err != nil {
				return fmt.Errorf("Zeile %d: %w", ln+1, err)
			}
			continue
		}

		if cur.section == "labels" && indent > 0 {
			c.Labels[key] = val
			continue
		}

		// Top-Level
		if val == "" {
			switch key {
			case "labels", "peers", "targets":
				flushTarget()
				cur = ctx{section: key, indent: indent}
				continue
			}
		}
		flushTarget()
		cur = ctx{}
		if err := setField(c, key, val); err != nil {
			return fmt.Errorf("Zeile %d: %w", ln+1, err)
		}
	}
	flushTarget()
	return nil
}

func unquote(s string) string {
	if len(s) >= 2 {
		if (s[0] == '"' && s[len(s)-1] == '"') || (s[0] == '\'' && s[len(s)-1] == '\'') {
			return s[1 : len(s)-1]
		}
	}
	if i := strings.Index(s, " #"); i >= 0 {
		s = strings.TrimSpace(s[:i])
	}
	return s
}

func setField(c *Config, key, val string) error {
	switch key {
	case "server_url", "url":
		c.ServerURL = val
	case "token":
		c.Token = val
	case "node_name":
		c.NodeName = val
	case "hostname":
		c.Hostname = val
	case "listen":
		c.ListenAddr = val
	case "interval":
		return setDur(&c.Interval, val)
	case "probe_interval":
		return setDur(&c.ProbeInterval, val)
	case "timeout":
		return setDur(&c.Timeout, val)
	case "buffer_batches":
		n, err := strconv.Atoi(val)
		if err != nil {
			return fmt.Errorf("buffer_batches: %w", err)
		}
		c.BufferBatches = n
	case "gpu":
		c.GPUEnabled = truthy(val)
	case "host":
		c.HostEnabled = truthy(val)
	case "discovery":
		c.DiscoveryEnabled = truthy(val)
	case "ollama_probe":
		c.OllamaProbe = truthy(val)
	case "report_addresses":
		c.ReportAddresses = truthy(val)
	case "ssh_user":
		c.SSHUser = val
	case "ssh_port":
		c.SSHPort = val
	case "unified_memory":
		c.UnifiedMemory = val
	case "tap_listen":
		c.TapListen = val
	case "tap_upstream":
		c.TapUpstream = val
	case "tap_service":
		c.TapService = val
	case "tap_kind":
		c.TapKind = val
	default:
		// Unbekannte Keys sind kein Fehler: eine neuere Konfiguration soll einen
		// älteren Agent nicht am Start hindern.
		return nil
	}
	return nil
}

func setTargetField(t *Target, key, val string) error {
	switch key {
	case "kind":
		t.Kind = val
	case "name":
		t.Name = val
	case "url":
		t.URL = val
	case "key":
		t.Key = val
	case "probe":
		t.Probe = val
	case "models":
		for _, m := range strings.Split(val, ",") {
			if m = strings.TrimSpace(m); m != "" {
				t.Models = append(t.Models, m)
			}
		}
	}
	return nil
}

func setDur(dst *time.Duration, val string) error {
	if d, err := time.ParseDuration(val); err == nil {
		*dst = d
		return nil
	}
	n, err := strconv.Atoi(val)
	if err != nil {
		return fmt.Errorf("keine Dauer: %q", val)
	}
	*dst = time.Duration(n) * time.Second
	return nil
}

func truthy(v string) bool {
	switch strings.ToLower(v) {
	case "1", "true", "yes", "on":
		return true
	}
	return false
}
