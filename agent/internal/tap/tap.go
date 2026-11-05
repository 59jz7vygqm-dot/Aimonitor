// Package tap ist ein Reverse-Proxy für den Inference-Pfad.
//
// Warum es das gibt: Time to First Token ist die Zahl, die Benutzer spüren,
// und sie lässt sich nur dort messen, wo die Bytes fließen. vLLM liefert
// Histogramme (ungenau, nicht nach Modell aufgelöst), Ollama liefert gar
// nichts. Der Tap sitzt zwischen OpenWebUI und den Backends, misst jeden
// Request und reicht ihn unverändert weiter.
//
// Der Tap darf den Datenpfad nie bremsen oder brechen:
//   - Streaming wird sofort durchgereicht (kein Puffern ganzer Antworten).
//   - Die Messung passiert nebenbei; Fehler beim Messen beeinflussen den
//     Request nicht.
//   - Ist der aimon-server weg, läuft der Proxy weiter.
package tap

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"io"
	"log"
	"net"
	"net/http"
	"net/http/httputil"
	"net/url"
	"strings"
	"sync"
	"time"

	"github.com/aimonitor/agent/internal/model"
)

type Sink interface {
	Enqueue(ctx context.Context, b model.Batch)
}

type Tap struct {
	Upstream    *url.URL
	ServiceKey  string
	NodeName    string
	Sink        Sink
	ClientLabel string

	proxy *httputil.ReverseProxy

	mu      sync.Mutex
	pending []model.Request
	events  []model.Event

	// Zählt gestreamte Requests, bei denen das Backend keine Tokenzahlen
	// mitgeschickt hat. Ohne die kann der Tap nur Zeichen zählen - ein
	// stiller Qualitätsverlust, der sonst niemandem auffällt.
	streamed        int
	streamedNoUsage int
	usageWarned     bool
}

func New(upstream, serviceKey, nodeName string, sink Sink) (*Tap, error) {
	u, err := url.Parse(upstream)
	if err != nil {
		return nil, err
	}
	t := &Tap{
		Upstream:    u,
		ServiceKey:  serviceKey,
		NodeName:    nodeName,
		Sink:        sink,
		ClientLabel: "tap",
	}
	t.proxy = &httputil.ReverseProxy{
		Director: func(r *http.Request) {
			r.URL.Scheme = u.Scheme
			r.URL.Host = u.Host
			if u.Path != "" && u.Path != "/" {
				r.URL.Path = strings.TrimRight(u.Path, "/") + r.URL.Path
			}
			r.Host = u.Host
		},
		FlushInterval: -1, // sofort durchreichen, sonst wäre TTFT sinnlos
		Transport: &http.Transport{
			Proxy: http.ProxyFromEnvironment,
			DialContext: (&net.Dialer{
				Timeout:   10 * time.Second,
				KeepAlive: 30 * time.Second,
			}).DialContext,
			MaxIdleConns:          100,
			IdleConnTimeout:       90 * time.Second,
			ResponseHeaderTimeout: 10 * time.Minute, // lange Prefill-Phasen
			ExpectContinueTimeout: 1 * time.Second,
		},
		ErrorHandler: func(w http.ResponseWriter, r *http.Request, err error) {
			log.Printf("tap: Upstream-Fehler %s: %v", r.URL.Path, err)
			http.Error(w, "upstream error: "+err.Error(), http.StatusBadGateway)
		},
	}
	return t, nil
}

func (t *Tap) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	if !isInferenceRoute(r.URL.Path) || r.Method != http.MethodPost {
		t.proxy.ServeHTTP(w, r)
		return
	}

	start := time.Now()
	meta := requestMeta{Route: r.URL.Path}

	// Body mitlesen, um Modell und Prompt-Länge zu kennen. Gedeckelt, damit ein
	// 100-MB-Body den Proxy nicht in den Speicher zieht.
	const maxInspect = 2 << 20
	if r.Body != nil {
		full, err := io.ReadAll(r.Body)
		r.Body.Close()
		if err != nil {
			http.Error(w, "request body unreadable", http.StatusBadRequest)
			return
		}
		meta.parse(full[:min(len(full), maxInspect)])
		meta.BodyBytes = len(full)
		r.Body = io.NopCloser(bytes.NewReader(full))
		r.ContentLength = int64(len(full))
	}

	client := r.Header.Get("X-Aimon-Client")
	if client == "" {
		client = guessClient(r.UserAgent())
	}

	rec := &recorder{ResponseWriter: w, start: start, stream: meta.Stream}
	t.proxy.ServeHTTP(rec, r)
	rec.Finish()

	total := float64(time.Since(start).Microseconds()) / 1000.0
	req := model.Request{
		Service: t.ServiceKey,
		TS:      float64(start.UnixNano()) / 1e9,
		Model:   meta.Model,
		TotalMs: model.F(total),
		Status:  rec.status,
		Stream:  meta.Stream,
		Client:  client,
		Route:   meta.Route,
	}
	if rec.ttft > 0 {
		req.TTFTMs = model.F(rec.ttft)
	}
	if meta.PromptChars > 0 {
		req.PromptChars = model.I(meta.PromptChars)
	}
	// Token-Zahlen nur übernehmen, wenn der Upstream sie wirklich geliefert hat.
	if rec.usage.PromptTokens > 0 {
		req.PromptTokens = model.I(rec.usage.PromptTokens)
	}
	if rec.usage.CompletionTokens > 0 {
		req.CompletionTokens = model.I(rec.usage.CompletionTokens)
	}
	if rec.status >= 400 {
		req.Error = rec.errSnippet
	}

	t.record(req)
}

func (t *Tap) record(r model.Request) {
	t.mu.Lock()
	t.pending = append(t.pending, r)
	n := len(t.pending)

	// Fehlende Tokenzahlen bei Streaming einmalig erklären. vLLM schickt das
	// usage-Objekt im Stream nur, wenn es dazu angewiesen wurde - sonst
	// stehen im Dashboard Zeichen statt Tokens, und man hält die Zahl für
	// eine Tokenzahl.
	if r.Stream && r.Status < 400 {
		t.streamed++
		if r.PromptTokens == nil {
			t.streamedNoUsage++
		}
		if !t.usageWarned && t.streamed >= 20 && t.streamedNoUsage == t.streamed {
			t.usageWarned = true
			t.events = append(t.events, model.Event{
				Kind:     "tap.no_token_counts",
				Severity: "info",
				Title:    "Backend schickt beim Streaming keine Tokenzahlen",
				TS:       float64(time.Now().UnixNano()) / 1e9,
				Service:  t.ServiceKey,
				Detail: map[string]any{
					"gestreamte_requests": t.streamed,
					"bedeutung": "Der Tap misst TTFT und Dauer exakt, kann " +
						"aber nur Zeichen statt Tokens zählen. Prompt-Längen " +
						"im Dashboard sind dann Zeichen, keine Tokens.",
					"abhilfe_vllm": "vLLM mit --enable-force-include-usage " +
						"starten; dann liegt das usage-Objekt auch im " +
						"gestreamten Antwortstrom.",
					"abhilfe_client": "Oder der Client setzt pro Anfrage " +
						"stream_options: {\"include_usage\": true}.",
				},
				DedupKey: "tap.no_token_counts:" + t.ServiceKey,
			})
		}
	}
	t.mu.Unlock()
	if n >= 50 {
		t.Flush(context.Background())
	}
}

// Flush gibt die gesammelten Requests an den Shipper weiter.
func (t *Tap) Flush(ctx context.Context) {
	t.mu.Lock()
	if len(t.pending) == 0 && len(t.events) == 0 {
		t.mu.Unlock()
		return
	}
	reqs := t.pending
	evs := t.events
	t.pending = nil
	t.events = nil
	t.mu.Unlock()

	t.Sink.Enqueue(ctx, model.Batch{
		Node:     model.Node{Name: t.NodeName},
		Requests: reqs,
		Events:   evs,
	})
}

func isInferenceRoute(p string) bool {
	switch {
	case strings.HasSuffix(p, "/chat/completions"),
		strings.HasSuffix(p, "/completions"),
		strings.HasSuffix(p, "/embeddings"),
		strings.HasSuffix(p, "/api/generate"),
		strings.HasSuffix(p, "/api/chat"),
		strings.HasSuffix(p, "/api/embed"),
		strings.HasSuffix(p, "/api/embeddings"):
		return true
	}
	return false
}

func guessClient(ua string) string {
	l := strings.ToLower(ua)
	switch {
	case strings.Contains(l, "open-webui"), strings.Contains(l, "openwebui"):
		return "openwebui"
	case strings.Contains(l, "python"), strings.Contains(l, "openai"):
		return "sdk"
	case strings.Contains(l, "curl"):
		return "curl"
	case ua == "":
		return "unbekannt"
	}
	if i := strings.IndexByte(ua, '/'); i > 0 {
		return ua[:i]
	}
	return ua
}

// ----------------------------------------------------------- Request-Analyse

type requestMeta struct {
	Model       string
	Stream      bool
	PromptChars int
	Route       string
	BodyBytes   int
}

func (m *requestMeta) parse(body []byte) {
	if len(body) == 0 {
		return
	}
	var v map[string]any
	if json.Unmarshal(body, &v) != nil {
		return
	}
	if s, ok := v["model"].(string); ok {
		m.Model = s
	}
	if b, ok := v["stream"].(bool); ok {
		m.Stream = b
	}
	// OpenAI-Format: messages[].content; Ollama: prompt
	if msgs, ok := v["messages"].([]any); ok {
		for _, raw := range msgs {
			mm, ok := raw.(map[string]any)
			if !ok {
				continue
			}
			switch c := mm["content"].(type) {
			case string:
				m.PromptChars += len(c)
			case []any:
				for _, part := range c {
					if pm, ok := part.(map[string]any); ok {
						if txt, ok := pm["text"].(string); ok {
							m.PromptChars += len(txt)
						}
					}
				}
			}
		}
	}
	if s, ok := v["prompt"].(string); ok {
		m.PromptChars += len(s)
	}
	if arr, ok := v["input"].([]any); ok {
		for _, x := range arr {
			if s, ok := x.(string); ok {
				m.PromptChars += len(s)
			}
		}
	} else if s, ok := v["input"].(string); ok {
		m.PromptChars += len(s)
	}
}

// --------------------------------------------------------- Antwort-Messung

type usage struct {
	PromptTokens     int `json:"prompt_tokens"`
	CompletionTokens int `json:"completion_tokens"`
	// Ollama nennt es anders:
	PromptEvalCount int `json:"prompt_eval_count"`
	EvalCount       int `json:"eval_count"`
}

type recorder struct {
	http.ResponseWriter
	start      time.Time
	stream     bool
	status     int
	ttft       float64
	wrote      bool
	usage      usage
	tail       []byte // letzte Bytes für die usage-Auswertung
	errSnippet string
}

func (r *recorder) WriteHeader(code int) {
	r.status = code
	r.ResponseWriter.WriteHeader(code)
}

func (r *recorder) Write(p []byte) (int, error) {
	if !r.wrote {
		r.wrote = true
		if r.status == 0 {
			r.status = 200
		}
		// Erstes Byte der Antwort = erstes Token bei Streaming. Bei nicht
		// gestreamten Antworten ist das die Gesamtlatenz, deshalb wird es
		// dort nicht als TTFT ausgewiesen.
		if r.stream {
			r.ttft = float64(time.Since(r.start).Microseconds()) / 1000.0
		}
	}
	r.scan(p)
	return r.ResponseWriter.Write(p)
}

// scan sucht die usage-Angaben, ohne die ganze Antwort zu puffern.
func (r *recorder) scan(p []byte) {
	const keep = 16 << 10
	r.tail = append(r.tail, p...)
	if len(r.tail) > keep {
		r.tail = r.tail[len(r.tail)-keep:]
	}
	if r.status >= 400 && r.errSnippet == "" {
		r.errSnippet = snippet(p)
	}
}

// Finish wertet den gepufferten Schwanz der Antwort aus. Muss nach dem
// Proxy-Durchlauf gerufen werden, bevor der Request gemeldet wird.
func (r *recorder) Finish() {
	r.parseUsage()
}

func (r *recorder) parseUsage() {
	if len(r.tail) == 0 {
		return
	}
	// Nicht gestreamt: ein JSON-Objekt.
	var direct struct {
		Usage           usage `json:"usage"`
		PromptEvalCount int   `json:"prompt_eval_count"`
		EvalCount       int   `json:"eval_count"`
	}
	if json.Unmarshal(r.tail, &direct) == nil {
		r.mergeUsage(direct.Usage)
		r.mergeUsage(usage{
			PromptEvalCount: direct.PromptEvalCount,
			EvalCount:       direct.EvalCount,
		})
		if r.usage.PromptTokens > 0 || r.usage.CompletionTokens > 0 {
			return
		}
	}
	// Gestreamt: SSE-Zeilen rückwärts durchgehen, der letzte Chunk mit usage gewinnt.
	sc := bufio.NewScanner(bytes.NewReader(r.tail))
	sc.Buffer(make([]byte, 0, 8192), 1<<20)
	for sc.Scan() {
		line := strings.TrimSpace(sc.Text())
		line = strings.TrimPrefix(line, "data:")
		line = strings.TrimSpace(line)
		if line == "" || line == "[DONE]" || !strings.HasPrefix(line, "{") {
			continue
		}
		var chunk struct {
			Usage           usage `json:"usage"`
			PromptEvalCount int   `json:"prompt_eval_count"`
			EvalCount       int   `json:"eval_count"`
		}
		if json.Unmarshal([]byte(line), &chunk) != nil {
			continue
		}
		r.mergeUsage(chunk.Usage)
		r.mergeUsage(usage{
			PromptEvalCount: chunk.PromptEvalCount,
			EvalCount:       chunk.EvalCount,
		})
	}
}

func (r *recorder) mergeUsage(u usage) {
	if u.PromptTokens > 0 {
		r.usage.PromptTokens = u.PromptTokens
	}
	if u.CompletionTokens > 0 {
		r.usage.CompletionTokens = u.CompletionTokens
	}
	if u.PromptEvalCount > 0 {
		r.usage.PromptTokens = u.PromptEvalCount
	}
	if u.EvalCount > 0 {
		r.usage.CompletionTokens = u.EvalCount
	}
}

// Flush reicht den Flush durch, sonst würde Streaming im Proxy hängen bleiben.
func (r *recorder) Flush() {
	if f, ok := r.ResponseWriter.(http.Flusher); ok {
		f.Flush()
	}
}

func (r *recorder) Unwrap() http.ResponseWriter { return r.ResponseWriter }

func snippet(p []byte) string {
	s := strings.TrimSpace(string(p))
	if len(s) > 200 {
		s = s[:200]
	}
	return strings.ReplaceAll(s, "\n", " ")
}

func min(a, b int) int {
	if a < b {
		return a
	}
	return b
}
