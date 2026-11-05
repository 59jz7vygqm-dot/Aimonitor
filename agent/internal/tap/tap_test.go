package tap

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"sync"
	"testing"
	"time"

	"github.com/aimonitor/agent/internal/model"
)

type memSink struct {
	mu     sync.Mutex
	reqs   []model.Request
	events []model.Event
}

func (m *memSink) Enqueue(_ context.Context, b model.Batch) {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.reqs = append(m.reqs, b.Requests...)
	m.events = append(m.events, b.Events...)
}

func (m *memSink) allEvents() []model.Event {
	m.mu.Lock()
	defer m.mu.Unlock()
	return append([]model.Event(nil), m.events...)
}

func (m *memSink) all() []model.Request {
	m.mu.Lock()
	defer m.mu.Unlock()
	return append([]model.Request(nil), m.reqs...)
}

// waitForRequests wartet, bis der Tap n Requests abgelegt hat.
//
// Nötig, weil der HTTP-Client die Antwort schon hat, bevor der Server-Handler
// die Messung fertigstellt - ohne Warten ist der Test flaky, nicht der Tap.
func waitForRequests(t *testing.T, tp *Tap, sink *memSink, n int) []model.Request {
	t.Helper()
	deadline := time.Now().Add(3 * time.Second)
	for {
		tp.Flush(context.Background())
		got := sink.all()
		if len(got) >= n {
			return got
		}
		if time.Now().After(deadline) {
			t.Fatalf("erwartet %d Requests, bekommen %d", n, len(got))
			return got
		}
		time.Sleep(5 * time.Millisecond)
	}
}

// Fake-vLLM: streamt SSE mit einer Verzögerung vor dem ersten Token.
func fakeStreamingBackend(prefill time.Duration) *httptest.Server {
	return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/event-stream")
		w.WriteHeader(200)
		fl, _ := w.(http.Flusher)
		time.Sleep(prefill)
		for i := 0; i < 3; i++ {
			fmt.Fprintf(w, "data: {\"choices\":[{\"delta\":{\"content\":\"tok%d\"}}]}\n\n", i)
			if fl != nil {
				fl.Flush()
			}
			time.Sleep(5 * time.Millisecond)
		}
		fmt.Fprint(w, "data: {\"choices\":[],\"usage\":{\"prompt_tokens\":1842,"+
			"\"completion_tokens\":311}}\n\n")
		fmt.Fprint(w, "data: [DONE]\n\n")
		if fl != nil {
			fl.Flush()
		}
	}))
}

func TestTapMeasuresStreamingTTFT(t *testing.T) {
	const prefill = 120 * time.Millisecond
	backend := fakeStreamingBackend(prefill)
	defer backend.Close()

	sink := &memSink{}
	tp, err := New(backend.URL, "vllm@test:8000", "test-node", sink)
	if err != nil {
		t.Fatal(err)
	}
	front := httptest.NewServer(tp)
	defer front.Close()

	body, _ := json.Marshal(map[string]any{
		"model":  "Qwen3-32B-AWQ",
		"stream": true,
		"messages": []map[string]string{
			{"role": "user", "content": "Hallo, wie geht es dir heute?"},
		},
	})
	req, _ := http.NewRequest("POST", front.URL+"/v1/chat/completions",
		bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("User-Agent", "open-webui/0.5.20")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	buf := new(bytes.Buffer)
	buf.ReadFrom(resp.Body)
	resp.Body.Close()

	if !bytes.Contains(buf.Bytes(), []byte("tok0")) {
		t.Fatalf("Antwort wurde nicht durchgereicht: %q", buf.String())
	}

	reqs := waitForRequests(t, tp, sink, 1)
	r := reqs[0]

	if r.TTFTMs == nil {
		t.Fatal("TTFT fehlt")
	}
	ttft := *r.TTFTMs
	// TTFT muss die Prefill-Zeit abbilden, aber nicht die Gesamtdauer.
	if ttft < 100 || ttft > 400 {
		t.Errorf("TTFT = %.1f ms, erwartet um %v", ttft, prefill)
	}
	if r.TotalMs == nil || *r.TotalMs <= ttft {
		t.Errorf("TotalMs (%v) muss größer als TTFT (%.1f) sein", r.TotalMs, ttft)
	}
	if r.Model != "Qwen3-32B-AWQ" {
		t.Errorf("Model = %q", r.Model)
	}
	if !r.Stream {
		t.Error("Stream sollte true sein")
	}
	if r.Client != "openwebui" {
		t.Errorf("Client = %q, erwartet openwebui", r.Client)
	}
	if r.Status != 200 {
		t.Errorf("Status = %d", r.Status)
	}
	if r.PromptTokens == nil || *r.PromptTokens != 1842 {
		t.Errorf("PromptTokens = %v, erwartet 1842 (aus usage im letzten Chunk)",
			r.PromptTokens)
	}
	if r.CompletionTokens == nil || *r.CompletionTokens != 311 {
		t.Errorf("CompletionTokens = %v, erwartet 311", r.CompletionTokens)
	}
	if r.PromptChars == nil || *r.PromptChars != len("Hallo, wie geht es dir heute?") {
		t.Errorf("PromptChars = %v", r.PromptChars)
	}
	if r.Service != "vllm@test:8000" {
		t.Errorf("Service = %q", r.Service)
	}
}

func TestTapNonStreamingHasNoTTFT(t *testing.T) {
	backend := httptest.NewServer(http.HandlerFunc(
		func(w http.ResponseWriter, r *http.Request) {
			w.Header().Set("Content-Type", "application/json")
			json.NewEncoder(w).Encode(map[string]any{
				"choices": []any{},
				"usage": map[string]int{
					"prompt_tokens": 50, "completion_tokens": 20,
				},
			})
		}))
	defer backend.Close()

	sink := &memSink{}
	tp, _ := New(backend.URL, "vllm@test:8000", "n", sink)
	front := httptest.NewServer(tp)
	defer front.Close()

	body, _ := json.Marshal(map[string]any{
		"model": "m", "stream": false,
		"messages": []map[string]string{{"role": "user", "content": "hi"}},
	})
	resp, err := http.Post(front.URL+"/v1/chat/completions", "application/json",
		bytes.NewReader(body))
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()

	reqs := waitForRequests(t, tp, sink, 1)
	// Ohne Streaming gibt es kein "erstes Token" - der Tap darf hier nichts
	// behaupten, sonst wäre die Gesamtlatenz als TTFT ausgewiesen.
	if reqs[0].TTFTMs != nil {
		t.Errorf("TTFT sollte nil sein, ist %v", *reqs[0].TTFTMs)
	}
	if reqs[0].PromptTokens == nil || *reqs[0].PromptTokens != 50 {
		t.Errorf("PromptTokens = %v", reqs[0].PromptTokens)
	}
}

func TestTapOllamaFormat(t *testing.T) {
	backend := httptest.NewServer(http.HandlerFunc(
		func(w http.ResponseWriter, r *http.Request) {
			fl, _ := w.(http.Flusher)
			time.Sleep(60 * time.Millisecond)
			fmt.Fprint(w, "{\"response\":\"a\",\"done\":false}\n")
			if fl != nil {
				fl.Flush()
			}
			fmt.Fprint(w, "{\"response\":\"\",\"done\":true,"+
				"\"prompt_eval_count\":77,\"eval_count\":12}\n")
			if fl != nil {
				fl.Flush()
			}
		}))
	defer backend.Close()

	sink := &memSink{}
	tp, _ := New(backend.URL, "ollama@test:11434", "n", sink)
	front := httptest.NewServer(tp)
	defer front.Close()

	body, _ := json.Marshal(map[string]any{
		"model": "gemma3:27b", "prompt": "hallo welt", "stream": true,
	})
	resp, err := http.Post(front.URL+"/api/generate", "application/json",
		bytes.NewReader(body))
	if err != nil {
		t.Fatal(err)
	}
	io := new(bytes.Buffer)
	io.ReadFrom(resp.Body)
	resp.Body.Close()

	reqs := waitForRequests(t, tp, sink, 1)
	r := reqs[0]
	if r.TTFTMs == nil || *r.TTFTMs < 50 {
		t.Errorf("TTFT = %v, erwartet >= 50 ms", r.TTFTMs)
	}
	// Ollama nennt die Tokenzahlen anders; der Tap muss beides verstehen.
	if r.PromptTokens == nil || *r.PromptTokens != 77 {
		t.Errorf("PromptTokens = %v, erwartet 77 (prompt_eval_count)", r.PromptTokens)
	}
	if r.CompletionTokens == nil || *r.CompletionTokens != 12 {
		t.Errorf("CompletionTokens = %v, erwartet 12 (eval_count)", r.CompletionTokens)
	}
	if r.Model != "gemma3:27b" {
		t.Errorf("Model = %q", r.Model)
	}
}

func TestTapPassesThroughNonInferenceRoutes(t *testing.T) {
	hit := false
	backend := httptest.NewServer(http.HandlerFunc(
		func(w http.ResponseWriter, r *http.Request) {
			hit = true
			w.Write([]byte(`{"data":[]}`))
		}))
	defer backend.Close()

	sink := &memSink{}
	tp, _ := New(backend.URL, "vllm@test:8000", "n", sink)
	front := httptest.NewServer(tp)
	defer front.Close()

	resp, err := http.Get(front.URL + "/v1/models")
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if !hit {
		t.Error("Request erreichte das Backend nicht")
	}
	tp.Flush(context.Background())
	if n := len(sink.all()); n != 0 {
		t.Errorf("/v1/models darf nicht als Inference-Request gezählt werden, got %d", n)
	}
}

func TestTapRecordsUpstreamFailure(t *testing.T) {
	backend := httptest.NewServer(http.HandlerFunc(
		func(w http.ResponseWriter, r *http.Request) {
			http.Error(w, `{"error":"model not found"}`, http.StatusNotFound)
		}))
	defer backend.Close()

	sink := &memSink{}
	tp, _ := New(backend.URL, "vllm@test:8000", "n", sink)
	front := httptest.NewServer(tp)
	defer front.Close()

	body, _ := json.Marshal(map[string]any{"model": "weg", "stream": true,
		"messages": []map[string]string{{"role": "user", "content": "x"}}})
	resp, _ := http.Post(front.URL+"/v1/chat/completions", "application/json",
		bytes.NewReader(body))
	if resp != nil {
		resp.Body.Close()
	}

	reqs := waitForRequests(t, tp, sink, 1)
	if reqs[0].Status != 404 {
		t.Errorf("Status = %d, erwartet 404", reqs[0].Status)
	}
	if reqs[0].Error == "" {
		t.Error("Error-Feld sollte gefüllt sein")
	}
}

// Backend, das streamt, aber kein usage-Objekt mitschickt - das Verhalten
// von vLLM ohne --enable-force-include-usage.
func streamingOhneUsage() *httptest.Server {
	return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/event-stream")
		w.WriteHeader(200)
		fl, _ := w.(http.Flusher)
		fmt.Fprint(w, "data: {\"choices\":[{\"delta\":{\"content\":\"x\"}}]}\n\n")
		if fl != nil {
			fl.Flush()
		}
		fmt.Fprint(w, "data: [DONE]\n\n")
		if fl != nil {
			fl.Flush()
		}
	}))
}

func sendeStream(t *testing.T, url string, n int) {
	t.Helper()
	body, _ := json.Marshal(map[string]any{
		"model": "m", "stream": true,
		"messages": []map[string]string{{"role": "user", "content": "hallo"}},
	})
	for i := 0; i < n; i++ {
		resp, err := http.Post(url+"/v1/chat/completions", "application/json",
			bytes.NewReader(body))
		if err != nil {
			t.Fatal(err)
		}
		io.Copy(io.Discard, resp.Body)
		resp.Body.Close()
	}
}

func TestTapMeldetFehlendeTokenzahlen(t *testing.T) {
	backend := streamingOhneUsage()
	defer backend.Close()
	sink := &memSink{}
	tp, _ := New(backend.URL, "vllm@test:8000", "n", sink)
	front := httptest.NewServer(tp)
	defer front.Close()

	sendeStream(t, front.URL, 25)
	reqs := waitForRequests(t, tp, sink, 25)

	// TTFT und Dauer werden weiter exakt gemessen – nur die Tokenzahlen fehlen.
	for _, r := range reqs {
		if r.TTFTMs == nil {
			t.Fatal("TTFT muss auch ohne usage gemessen werden")
		}
		if r.PromptTokens != nil {
			t.Fatal("ohne usage darf keine Tokenzahl erfunden werden")
		}
		if r.PromptChars == nil || *r.PromptChars != len("hallo") {
			t.Errorf("PromptChars = %v – der Zeichen-Ersatz fehlt", r.PromptChars)
		}
	}

	// Genau ein erklärender Hinweis, nicht einer pro Request.
	evs := sink.allEvents()
	if len(evs) != 1 {
		t.Fatalf("erwartet genau 1 Hinweis, bekommen %d", len(evs))
	}
	if evs[0].Kind != "tap.no_token_counts" {
		t.Errorf("Kind = %q", evs[0].Kind)
	}
	if d, _ := evs[0].Detail["abhilfe_vllm"].(string); d == "" ||
		!bytes.Contains([]byte(d), []byte("--enable-force-include-usage")) {
		t.Errorf("Der Hinweis muss den Schalter nennen: %v", evs[0].Detail)
	}

	// Und auch bei weiterem Verkehr nicht erneut.
	sendeStream(t, front.URL, 10)
	waitForRequests(t, tp, sink, 35)
	if n := len(sink.allEvents()); n != 1 {
		t.Errorf("Hinweis wurde %d-mal geschickt", n)
	}
}

func TestTapSchweigtWennTokenzahlenKommen(t *testing.T) {
	backend := fakeStreamingBackend(5 * time.Millisecond)
	defer backend.Close()
	sink := &memSink{}
	tp, _ := New(backend.URL, "vllm@test:8000", "n", sink)
	front := httptest.NewServer(tp)
	defer front.Close()

	sendeStream(t, front.URL, 25)
	reqs := waitForRequests(t, tp, sink, 25)
	if reqs[0].PromptTokens == nil {
		t.Fatal("mit usage müssen Tokenzahlen ankommen")
	}
	if n := len(sink.allEvents()); n != 0 {
		t.Errorf("kein Hinweis nötig, es kamen aber %d", n)
	}
}
