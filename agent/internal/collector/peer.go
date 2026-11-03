package collector

import (
	"context"
	"fmt"
	"net"
	"net/http"
	"strings"
	"time"

	"github.com/aimonitor/agent/internal/model"
)

// PeerProbe prüft Ziele außerhalb dieses Hosts. Damit entsteht die
// Konnektivitätsmatrix: wer erreicht wen, mit welcher Latenz.
type PeerProbe struct {
	Targets []string
	HTTP    *http.Client
	Timeout time.Duration
}

func (p *PeerProbe) Name() string { return "peers" }

func (p *PeerProbe) Collect(ctx context.Context, now float64) (model.Batch, error) {
	var b model.Batch
	for _, raw := range p.Targets {
		pr := p.probeOne(ctx, raw, now)
		b.Probes = append(b.Probes, pr)
		b.Samples = append(b.Samples, model.Sample{
			Metric: "probe.up", Value: boolVal(pr.OK), TS: now,
			Labels: map[string]string{"target": raw},
		})
		if pr.LatencyMs != nil {
			b.Samples = append(b.Samples, model.Sample{
				Metric: "probe.latency_ms", Value: *pr.LatencyMs, TS: now,
				Labels: map[string]string{"target": raw},
			})
		}
	}
	return b, nil
}

func (p *PeerProbe) probeOne(ctx context.Context, raw string, now float64) model.Probe {
	pr := model.Probe{Target: raw, TS: now}
	start := time.Now()

	if !strings.HasPrefix(raw, "http://") && !strings.HasPrefix(raw, "https://") {
		// Reines host:port -> TCP-Verbindungstest. Sagt nur, ob der Port offen ist.
		d := net.Dialer{Timeout: p.Timeout}
		conn, err := d.DialContext(ctx, "tcp", raw)
		if err != nil {
			pr.Error = shortErr(err)
			return pr
		}
		conn.Close()
		pr.OK = true
		pr.LatencyMs = model.F(float64(time.Since(start).Microseconds()) / 1000.0)
		return pr
	}

	req, err := http.NewRequestWithContext(ctx, http.MethodGet, raw, nil)
	if err != nil {
		pr.Error = shortErr(err)
		return pr
	}
	req.Header.Set("User-Agent", "aimon-agent")
	resp, err := p.HTTP.Do(req)
	if err != nil {
		pr.Error = shortErr(err)
		return pr
	}
	resp.Body.Close()
	pr.HTTPStatus = resp.StatusCode
	pr.OK = resp.StatusCode < 400
	pr.LatencyMs = model.F(float64(time.Since(start).Microseconds()) / 1000.0)
	if !pr.OK {
		pr.Error = fmt.Sprintf("HTTP %d", resp.StatusCode)
	}
	return pr
}
