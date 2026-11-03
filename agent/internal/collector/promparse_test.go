package collector

import (
	"math"
	"strings"
	"testing"
)

// Auszug aus einem echten vLLM-/metrics-Endpunkt.
const vllmMetrics = `
# HELP vllm:num_requests_running Number of requests currently running on GPU.
# TYPE vllm:num_requests_running gauge
vllm:num_requests_running{model_name="Qwen3-32B-AWQ"} 3.0
# HELP vllm:num_requests_waiting Number of requests waiting to be processed.
# TYPE vllm:num_requests_waiting gauge
vllm:num_requests_waiting{model_name="Qwen3-32B-AWQ"} 0.0
# HELP vllm:gpu_cache_usage_perc GPU KV-cache usage. 1 means 100 percent usage.
# TYPE vllm:gpu_cache_usage_perc gauge
vllm:gpu_cache_usage_perc{model_name="Qwen3-32B-AWQ"} 0.4123
# HELP vllm:time_to_first_token_seconds Histogram of time to first token in seconds.
# TYPE vllm:time_to_first_token_seconds histogram
vllm:time_to_first_token_seconds_bucket{le="0.001",model_name="Qwen3-32B-AWQ"} 0.0
vllm:time_to_first_token_seconds_bucket{le="0.1",model_name="Qwen3-32B-AWQ"} 12.0
vllm:time_to_first_token_seconds_bucket{le="0.5",model_name="Qwen3-32B-AWQ"} 180.0
vllm:time_to_first_token_seconds_bucket{le="+Inf",model_name="Qwen3-32B-AWQ"} 201.0
vllm:time_to_first_token_seconds_sum{model_name="Qwen3-32B-AWQ"} 45.231
vllm:time_to_first_token_seconds_count{model_name="Qwen3-32B-AWQ"} 201.0
# TYPE vllm:prompt_tokens_total counter
vllm:prompt_tokens_total{model_name="Qwen3-32B-AWQ"} 412033.0
vllm:request_success_total{finished_reason="stop",model_name="Qwen3-32B-AWQ"} 198.0
vllm:request_success_total{finished_reason="length",model_name="Qwen3-32B-AWQ"} 3.0
metric_with_timestamp 1.5 1693412000000
metric_escaped{path="a\"b",nl="x\ny"} 7
metric_nan NaN
`

func TestParsePromText(t *testing.T) {
	ss, err := ParsePromText(strings.NewReader(vllmMetrics))
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	idx := NewPromIndex(ss)

	if s, ok := idx.First("vllm:num_requests_running"); !ok || s.Value != 3 {
		t.Errorf("num_requests_running = %+v ok=%v", s, ok)
	}
	if s, ok := idx.First("vllm:gpu_cache_usage_perc"); !ok || math.Abs(s.Value-0.4123) > 1e-9 {
		t.Errorf("gpu_cache_usage_perc = %+v", s)
	}
	if s, _ := idx.First("vllm:num_requests_running"); s.Labels["model_name"] != "Qwen3-32B-AWQ" {
		t.Errorf("labels = %v", s.Labels)
	}

	buckets := idx["vllm:time_to_first_token_seconds_bucket"]
	if len(buckets) != 4 {
		t.Fatalf("buckets = %d, erwartet 4", len(buckets))
	}
	var inf bool
	for _, b := range buckets {
		if b.Labels["le"] == "+Inf" {
			inf = true
			if b.Value != 201 {
				t.Errorf("+Inf-Bucket = %v", b.Value)
			}
		}
	}
	if !inf {
		t.Error("+Inf-Bucket fehlt")
	}

	// request_success_total hat zwei Serien -> Sum muss addieren
	if v, ok := idx.Sum("vllm:request_success_total"); !ok || v != 201 {
		t.Errorf("Sum(request_success_total) = %v ok=%v", v, ok)
	}

	// Zeitstempel in der Zeile wird ignoriert, Wert übernommen
	if s, ok := idx.First("metric_with_timestamp"); !ok || s.Value != 1.5 {
		t.Errorf("metric_with_timestamp = %+v", s)
	}

	// Escapes in Labelwerten
	if s, ok := idx.First("metric_escaped"); !ok {
		t.Error("metric_escaped fehlt")
	} else if s.Labels["path"] != `a"b` || s.Labels["nl"] != "x\ny" {
		t.Errorf("Escapes falsch: %v", s.Labels)
	}

	// NaN muss verworfen werden, der Server akzeptiert es nicht
	if _, ok := idx.First("metric_nan"); ok {
		t.Error("NaN-Zeile hätte verworfen werden müssen")
	}
}

func TestParsePromTextNoLabels(t *testing.T) {
	ss, err := ParsePromText(strings.NewReader("go_goroutines 42\nup 1\n"))
	if err != nil {
		t.Fatal(err)
	}
	if len(ss) != 2 || ss[0].Name != "go_goroutines" || ss[0].Value != 42 {
		t.Errorf("got %+v", ss)
	}
}

func TestParsePromTextInf(t *testing.T) {
	ss, _ := ParsePromText(strings.NewReader("x +Inf\ny -Inf\n"))
	if len(ss) != 2 {
		t.Fatalf("got %+v", ss)
	}
	if !math.IsInf(ss[0].Value, 1) || !math.IsInf(ss[1].Value, -1) {
		t.Errorf("Inf nicht erkannt: %+v", ss)
	}
}
