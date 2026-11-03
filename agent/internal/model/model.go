// Package model spiegelt den Ingest-Contract (docs/integration-contract.md).
package model

type Node struct {
	Name         string            `json:"name"`
	Hostname     string            `json:"hostname,omitempty"`
	AgentVersion string            `json:"agent_version,omitempty"`
	Labels       map[string]string `json:"labels,omitempty"`
}

type Service struct {
	Key     string            `json:"key"`
	Kind    string            `json:"kind"`
	Name    string            `json:"name,omitempty"`
	BaseURL string            `json:"base_url,omitempty"`
	Version string            `json:"version,omitempty"`
	Labels  map[string]string `json:"labels,omitempty"`
}

type Sample struct {
	Metric  string            `json:"metric"`
	Value   float64           `json:"value"`
	TS      float64           `json:"ts,omitempty"`
	Labels  map[string]string `json:"labels,omitempty"`
	Service string            `json:"service,omitempty"`
}

type Request struct {
	Service          string   `json:"service"`
	TS               float64  `json:"ts,omitempty"`
	Model            string   `json:"model,omitempty"`
	TTFTMs           *float64 `json:"ttft_ms,omitempty"`
	TotalMs          *float64 `json:"total_ms,omitempty"`
	QueueMs          *float64 `json:"queue_ms,omitempty"`
	PromptTokens     *int     `json:"prompt_tokens,omitempty"`
	PromptChars      *int     `json:"prompt_chars,omitempty"`
	CompletionTokens *int     `json:"completion_tokens,omitempty"`
	Status           int      `json:"status,omitempty"`
	Stream           bool     `json:"stream,omitempty"`
	Client           string   `json:"client,omitempty"`
	Route            string   `json:"route,omitempty"`
	Error            string   `json:"error,omitempty"`
}

type Probe struct {
	Target        string   `json:"target"`
	OK            bool     `json:"ok"`
	TS            float64  `json:"ts,omitempty"`
	TargetService string   `json:"target_service,omitempty"`
	SourceService string   `json:"source_service,omitempty"`
	LatencyMs     *float64 `json:"latency_ms,omitempty"`
	HTTPStatus    int      `json:"http_status,omitempty"`
	Error         string   `json:"error,omitempty"`
}

type Event struct {
	Kind     string         `json:"kind"`
	Severity string         `json:"severity"`
	Title    string         `json:"title"`
	TS       float64        `json:"ts,omitempty"`
	Service  string         `json:"service,omitempty"`
	Detail   map[string]any `json:"detail,omitempty"`
	DedupKey string         `json:"dedup_key,omitempty"`
}

type Batch struct {
	Node     Node      `json:"node"`
	Services []Service `json:"services,omitempty"`
	Samples  []Sample  `json:"samples,omitempty"`
	Requests []Request `json:"requests,omitempty"`
	Probes   []Probe   `json:"probes,omitempty"`
	Events   []Event   `json:"events,omitempty"`
}

func (b *Batch) Len() int {
	return len(b.Services) + len(b.Samples) + len(b.Requests) + len(b.Probes) + len(b.Events)
}

// Merge hängt einen anderen Batch an. Node wird nicht überschrieben.
func (b *Batch) Merge(o Batch) {
	b.Services = append(b.Services, o.Services...)
	b.Samples = append(b.Samples, o.Samples...)
	b.Requests = append(b.Requests, o.Requests...)
	b.Probes = append(b.Probes, o.Probes...)
	b.Events = append(b.Events, o.Events...)
}

func F(v float64) *float64 { return &v }
func I(v int) *int         { return &v }
