package collector

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"math"
	"net/http"
	"strconv"
	"strings"
	"time"

	"github.com/aimonitor/agent/internal/config"
	"github.com/aimonitor/agent/internal/model"
)

// ServiceCollector fragt einen einzelnen Dienst ab. Welche Metriken es gibt,
// hängt von `Kind` ab. Jeder Dienst liefert mindestens eine Erreichbarkeitsprobe.
type ServiceCollector struct {
	Target config.Target
	HTTP   *http.Client
	Node   string

	consecutiveFails int
}

func (s *ServiceCollector) Name() string { return "service:" + s.Target.Key }

func (s *ServiceCollector) Collect(ctx context.Context, now float64) (model.Batch, error) {
	var b model.Batch
	t := s.Target

	svc := model.Service{
		Key:     t.Key,
		Kind:    t.Kind,
		Name:    t.Name,
		BaseURL: t.URL,
		Labels:  t.Labels,
	}

	probePath := t.Probe
	if probePath == "" {
		probePath = defaultProbePath(t.Kind)
	}
	start := time.Now()
	status, body, err := s.get(ctx, probePath)
	latency := float64(time.Since(start).Microseconds()) / 1000.0
	ok := err == nil && status > 0 && status < 400

	p := model.Probe{
		Target:        strings.TrimRight(t.URL, "/") + probePath,
		TargetService: t.Key,
		OK:            ok,
		TS:            now,
		HTTPStatus:    status,
	}
	if ok {
		p.LatencyMs = model.F(latency)
		s.consecutiveFails = 0
	} else {
		s.consecutiveFails++
		if err != nil {
			p.Error = shortErr(err)
		} else {
			p.Error = fmt.Sprintf("HTTP %d", status)
		}
	}
	b.Probes = append(b.Probes, p)
	b.Samples = append(b.Samples,
		model.Sample{Metric: "probe.up", Value: boolVal(ok), TS: now,
			Labels: map[string]string{"target": t.Key}},
	)
	if ok {
		b.Samples = append(b.Samples,
			model.Sample{Metric: "probe.latency_ms", Value: latency, TS: now,
				Labels: map[string]string{"target": t.Key}},
		)
	}

	if !ok {
		b.Services = append(b.Services, svc)
		return b, nil
	}

	switch t.Kind {
	case "vllm":
		s.collectVLLM(ctx, now, &b, &svc, body)
	case "ollama":
		s.collectOllama(ctx, now, &b, &svc)
	case "comfyui":
		s.collectComfy(ctx, now, &b, &svc)
	case "openwebui":
		b.Samples = append(b.Samples,
			model.Sample{Metric: "openwebui.up", Value: 1, TS: now, Service: t.Key},
			model.Sample{Metric: "openwebui.response_ms", Value: latency, TS: now,
				Service: t.Key},
		)
		svc.Version = jsonString(body, "version")
	case "searxng":
		b.Samples = append(b.Samples,
			model.Sample{Metric: "searxng.up", Value: 1, TS: now, Service: t.Key},
			model.Sample{Metric: "searxng.response_ms", Value: latency, TS: now,
				Service: t.Key},
		)
	default:
		b.Samples = append(b.Samples,
			model.Sample{Metric: "service.up", Value: 1, TS: now, Service: t.Key},
			model.Sample{Metric: "service.response_ms", Value: latency, TS: now,
				Service: t.Key},
		)
	}

	b.Services = append(b.Services, svc)
	return b, nil
}

func defaultProbePath(kind string) string {
	switch kind {
	case "vllm":
		return "/metrics"
	case "ollama":
		return "/api/version"
	case "comfyui":
		return "/system_stats"
	case "openwebui":
		return "/health"
	case "searxng":
		return "/healthz"
	default:
		return "/"
	}
}

// ------------------------------------------------------------------ vLLM

// vllmMap bildet vLLM-Metriknamen auf aimonitor-Namen ab.
// Wert ist {Zielmetrik, Skalierung}.
var vllmGauges = map[string]struct {
	name  string
	scale float64
}{
	"vllm:num_requests_running":      {"llm.requests_running", 1},
	"vllm:num_requests_waiting":      {"llm.requests_waiting", 1},
	"vllm:num_requests_swapped":      {"llm.requests_swapped", 1},
	"vllm:gpu_cache_usage_perc":      {"llm.kv_cache_usage_pct", 100},
	"vllm:cpu_cache_usage_perc":      {"llm.kv_cache_cpu_usage_pct", 100},
	"vllm:gpu_prefix_cache_hit_rate": {"llm.prefix_cache_hit_pct", 100},
	"vllm:lora_requests_info":        {"llm.lora_info", 1},
}

var vllmCounters = map[string]string{
	"vllm:prompt_tokens_total":     "llm.prompt_tokens_total",
	"vllm:generation_tokens_total": "llm.generation_tokens_total",
	"vllm:request_success_total":   "llm.requests_total",
	"vllm:num_preemptions_total":   "llm.preemptions_total",
	"vllm:iteration_tokens_total":  "llm.iteration_tokens_total",
}

// vllmHistograms: vLLM-Präfix -> aimonitor-Präfix.
var vllmHistograms = map[string]string{
	"vllm:time_to_first_token_seconds":    "llm.ttft",
	"vllm:e2e_request_latency_seconds":    "llm.e2e",
	"vllm:time_per_output_token_seconds":  "llm.tpot",
	"vllm:request_queue_time_seconds":     "llm.queue_time",
	"vllm:request_inference_time_seconds": "llm.inference_time",
	"vllm:request_prompt_tokens":          "llm.prompt_len",
	"vllm:request_generation_tokens":      "llm.gen_len",
}

func (s *ServiceCollector) collectVLLM(
	ctx context.Context, now float64, b *model.Batch, svc *model.Service, body []byte,
) {
	// Die Probe ging bereits auf /metrics, also ist body der Metriktext.
	if len(body) == 0 {
		_, body2, err := s.get(ctx, "/metrics")
		if err != nil {
			return
		}
		body = body2
	}
	samples, err := ParsePromText(strings.NewReader(string(body)))
	if err != nil || len(samples) == 0 {
		b.Events = append(b.Events, model.Event{
			Kind: "agent.parse_failed", Severity: "warning",
			Title:    "vLLM-/metrics nicht auswertbar",
			TS:       now,
			Service:  s.Target.Key,
			Detail:   map[string]any{"bytes": len(body)},
			DedupKey: "agent.parse_failed:" + s.Target.Key,
		})
		return
	}
	idx := NewPromIndex(samples)

	// Modellname aus den Labels übernehmen: er beantwortet die Frage
	// "welches Modell läuft da eigentlich" ohne Rückfrage.
	modelName := ""
	for _, ss := range samples {
		if m := ss.Labels["model_name"]; m != "" {
			modelName = m
			break
		}
	}
	if modelName != "" {
		if svc.Labels == nil {
			svc.Labels = map[string]string{}
		}
		svc.Labels["model"] = modelName
	}

	key := s.Target.Key
	emit := func(metric string, v float64, labels map[string]string) {
		if math.IsNaN(v) || math.IsInf(v, 0) {
			return
		}
		b.Samples = append(b.Samples, model.Sample{
			Metric: metric, Value: v, TS: now, Service: key, Labels: labels,
		})
	}

	for src, dst := range vllmGauges {
		if v, ok := idx.Sum(src); ok {
			emit(dst.name, v*dst.scale, nil)
		}
	}
	for src, dst := range vllmCounters {
		if src == "vllm:request_success_total" {
			// nach finished_reason aufgeschlüsselt lassen: "length" vs "stop"
			// unterscheidet abgeschnittene von normalen Antworten.
			for _, ss := range idx[src] {
				lbl := map[string]string{}
				if r := ss.Labels["finished_reason"]; r != "" {
					lbl["finished_reason"] = r
				}
				emit(dst, ss.Value, lbl)
			}
			continue
		}
		if v, ok := idx.Sum(src); ok {
			emit(dst, v, nil)
		}
	}

	for src, dst := range vllmHistograms {
		for _, ss := range idx[src+"_bucket"] {
			le := ss.Labels["le"]
			if le == "" {
				continue
			}
			emit(dst+"_bucket", ss.Value, map[string]string{"le": le})
		}
		if v, ok := idx.Sum(src + "_sum"); ok {
			suffix := "_sum_s"
			if strings.HasSuffix(dst, "_len") {
				suffix = "_sum"
			}
			emit(dst+suffix, v, nil)
		}
		if v, ok := idx.Sum(src + "_count"); ok {
			emit(dst+"_count", v, nil)
		}
	}

	// Version, wenn vLLM sie als Info-Gauge ausgibt.
	if ss, ok := idx.First("vllm:cache_config_info"); ok {
		if bs := ss.Labels["block_size"]; bs != "" {
			if svc.Labels == nil {
				svc.Labels = map[string]string{}
			}
			svc.Labels["block_size"] = bs
		}
	}
	if v := jsonlessVersion(ctx, s); v != "" {
		svc.Version = v
	}
}

func jsonlessVersion(ctx context.Context, s *ServiceCollector) string {
	st, body, err := s.get(ctx, "/version")
	if err != nil || st != 200 {
		return ""
	}
	return jsonString(body, "version")
}

// ----------------------------------------------------------------- Ollama

type ollamaPS struct {
	Models []struct {
		Name      string    `json:"name"`
		Model     string    `json:"model"`
		Size      int64     `json:"size"`
		SizeVRAM  int64     `json:"size_vram"`
		ExpiresAt time.Time `json:"expires_at"`
		Details   struct {
			ParameterSize     string `json:"parameter_size"`
			QuantizationLevel string `json:"quantization_level"`
		} `json:"details"`
	} `json:"models"`
}

func (s *ServiceCollector) collectOllama(
	ctx context.Context, now float64, b *model.Batch, svc *model.Service,
) {
	key := s.Target.Key

	if st, body, err := s.get(ctx, "/api/version"); err == nil && st == 200 {
		svc.Version = jsonString(body, "version")
	}

	st, body, err := s.get(ctx, "/api/ps")
	if err != nil || st != 200 {
		b.Samples = append(b.Samples, model.Sample{
			Metric: "ollama.models_loaded", Value: 0, TS: now, Service: key,
		})
		return
	}
	var ps ollamaPS
	if err := json.Unmarshal(body, &ps); err != nil {
		return
	}
	b.Samples = append(b.Samples, model.Sample{
		Metric: "ollama.models_loaded", Value: float64(len(ps.Models)),
		TS: now, Service: key,
	})
	for _, m := range ps.Models {
		name := m.Name
		if name == "" {
			name = m.Model
		}
		lbl := map[string]string{"model": name}
		if m.Details.ParameterSize != "" {
			lbl["params"] = m.Details.ParameterSize
		}
		if m.Details.QuantizationLevel != "" {
			lbl["quant"] = m.Details.QuantizationLevel
		}
		b.Samples = append(b.Samples,
			model.Sample{Metric: "ollama.model_loaded", Value: 1, TS: now,
				Service: key, Labels: lbl},
			model.Sample{Metric: "ollama.model_vram_bytes", Value: float64(m.SizeVRAM),
				TS: now, Service: key, Labels: lbl},
			model.Sample{Metric: "ollama.model_size_bytes", Value: float64(m.Size),
				TS: now, Service: key, Labels: lbl},
		)
		if !m.ExpiresAt.IsZero() {
			b.Samples = append(b.Samples, model.Sample{
				Metric: "ollama.model_expires_in_s",
				Value:  time.Until(m.ExpiresAt).Seconds(),
				TS:     now, Service: key, Labels: lbl,
			})
		}
	}

	// Verfügbare (nicht geladene) Modelle zählen.
	if st, body, err := s.get(ctx, "/api/tags"); err == nil && st == 200 {
		var tags struct {
			Models []struct{ Name string } `json:"models"`
		}
		if json.Unmarshal(body, &tags) == nil {
			b.Samples = append(b.Samples, model.Sample{
				Metric: "ollama.models_available", Value: float64(len(tags.Models)),
				TS: now, Service: key,
			})
		}
	}
}

// ProbeOllamaTTFT misst mit einem 1-Token-Request, wie schnell ein Modell
// antwortet. Das ist eine Bereitschaftsmessung, keine Benutzerlatenz - echte
// TTFT pro Request gibt es nur über aimon-tap.
func (s *ServiceCollector) ProbeOllamaTTFT(
	ctx context.Context, now float64, modelName string,
) model.Batch {
	var b model.Batch
	key := s.Target.Key
	lbl := map[string]string{"model": modelName, "synthetic": "true"}

	payload, _ := json.Marshal(map[string]any{
		"model":   modelName,
		"prompt":  "ping",
		"stream":  true,
		"options": map[string]any{"num_predict": 1, "temperature": 0},
	})
	req, err := http.NewRequestWithContext(ctx, http.MethodPost,
		strings.TrimRight(s.Target.URL, "/")+"/api/generate", strings.NewReader(string(payload)))
	if err != nil {
		return b
	}
	req.Header.Set("Content-Type", "application/json")

	start := time.Now()
	resp, err := s.HTTP.Do(req)
	if err != nil {
		b.Samples = append(b.Samples, model.Sample{
			Metric: "ollama.probe_ok", Value: 0, TS: now, Service: key, Labels: lbl,
		})
		return b
	}
	defer resp.Body.Close()

	buf := make([]byte, 1)
	var ttft float64 = -1
	acc := make([]byte, 0, 256)
	for {
		n, err := resp.Body.Read(buf)
		if n > 0 {
			acc = append(acc, buf[0])
			if buf[0] == '\n' && ttft < 0 {
				ttft = float64(time.Since(start).Microseconds()) / 1000.0
				break
			}
		}
		if err != nil {
			break
		}
		if len(acc) > 8192 {
			break
		}
	}
	io.Copy(io.Discard, io.LimitReader(resp.Body, 1<<20))

	if ttft >= 0 {
		b.Samples = append(b.Samples,
			model.Sample{Metric: "ollama.probe_ttft_ms", Value: ttft, TS: now,
				Service: key, Labels: lbl},
			model.Sample{Metric: "ollama.probe_ok", Value: 1, TS: now,
				Service: key, Labels: lbl},
		)
	} else {
		b.Samples = append(b.Samples, model.Sample{
			Metric: "ollama.probe_ok", Value: 0, TS: now, Service: key, Labels: lbl,
		})
	}
	return b
}

// ---------------------------------------------------------------- ComfyUI

func (s *ServiceCollector) collectComfy(
	ctx context.Context, now float64, b *model.Batch, svc *model.Service,
) {
	key := s.Target.Key

	st, body, err := s.get(ctx, "/system_stats")
	if err == nil && st == 200 {
		var stats struct {
			System struct {
				ComfyUIVersion string `json:"comfyui_version"`
				PythonVersion  string `json:"python_version"`
			} `json:"system"`
			Devices []struct {
				Name           string  `json:"name"`
				Type           string  `json:"type"`
				VRAMTotal      float64 `json:"vram_total"`
				VRAMFree       float64 `json:"vram_free"`
				TorchVRAMTotal float64 `json:"torch_vram_total"`
				TorchVRAMFree  float64 `json:"torch_vram_free"`
			} `json:"devices"`
		}
		if json.Unmarshal(body, &stats) == nil {
			svc.Version = stats.System.ComfyUIVersion
			for i, d := range stats.Devices {
				lbl := map[string]string{"device": strconv.Itoa(i)}
				if d.Name != "" {
					lbl["name"] = d.Name
				}
				b.Samples = append(b.Samples,
					model.Sample{Metric: "comfy.vram_total_bytes", Value: d.VRAMTotal,
						TS: now, Service: key, Labels: lbl},
					model.Sample{Metric: "comfy.vram_free_bytes", Value: d.VRAMFree,
						TS: now, Service: key, Labels: lbl},
					model.Sample{Metric: "comfy.torch_vram_free_bytes",
						Value: d.TorchVRAMFree, TS: now, Service: key, Labels: lbl},
				)
			}
		}
	}

	if st, body, err := s.get(ctx, "/queue"); err == nil && st == 200 {
		var q struct {
			Running []json.RawMessage `json:"queue_running"`
			Pending []json.RawMessage `json:"queue_pending"`
		}
		if json.Unmarshal(body, &q) == nil {
			b.Samples = append(b.Samples,
				model.Sample{Metric: "comfy.queue_running", Value: float64(len(q.Running)),
					TS: now, Service: key},
				model.Sample{Metric: "comfy.queue_pending", Value: float64(len(q.Pending)),
					TS: now, Service: key},
			)
		}
	}

	b.Samples = append(b.Samples, model.Sample{
		Metric: "comfy.up", Value: 1, TS: now, Service: key,
	})
}

// -------------------------------------------------------------- Hilfsmittel

func (s *ServiceCollector) get(ctx context.Context, path string) (int, []byte, error) {
	url := strings.TrimRight(s.Target.URL, "/") + path
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return 0, nil, err
	}
	req.Header.Set("User-Agent", "aimon-agent")
	resp, err := s.HTTP.Do(req)
	if err != nil {
		return 0, nil, err
	}
	defer resp.Body.Close()
	// 8 MB reichen für jeden /metrics-Endpunkt und deckeln den Speicher.
	body, err := io.ReadAll(io.LimitReader(resp.Body, 8<<20))
	return resp.StatusCode, body, err
}

func jsonString(body []byte, key string) string {
	var m map[string]any
	if json.Unmarshal(body, &m) != nil {
		return ""
	}
	if v, ok := m[key].(string); ok {
		return v
	}
	return ""
}

func boolVal(b bool) float64 {
	if b {
		return 1
	}
	return 0
}

func shortErr(err error) string {
	s := err.Error()
	if i := strings.LastIndex(s, ": "); i >= 0 && len(s)-i < 60 {
		s = s[i+2:]
	}
	if len(s) > 200 {
		s = s[:200]
	}
	return s
}
