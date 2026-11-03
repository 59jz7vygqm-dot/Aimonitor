// Package shipper schickt Batches an den aimon-server.
//
// Grundsatz: der Agent darf den Host nie stören. Ist der Server weg, wandern
// Batches in einen Ringpuffer; läuft der über, fallen die ältesten raus.
// Speicherverbrauch ist dadurch nach oben begrenzt.
package shipper

import (
	"bytes"
	"compress/gzip"
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"math"
	"net/http"
	"strings"
	"sync"
	"time"

	"github.com/aimonitor/agent/internal/model"
)

type Stats struct {
	Sent        int64
	Failed      int64
	Dropped     int64
	Buffered    int
	LastError   string
	LastSuccess time.Time
}

type Shipper struct {
	URL     string
	Token   string
	Timeout time.Duration
	Client  *http.Client

	mu     sync.Mutex
	buf    []model.Batch
	maxBuf int
	stats  Stats

	backoff time.Duration
	nextTry time.Time
}

func New(url, token string, timeout time.Duration, maxBuf int) *Shipper {
	if maxBuf < 1 {
		maxBuf = 1
	}
	return &Shipper{
		URL:     strings.TrimRight(url, "/"),
		Token:   token,
		Timeout: timeout,
		Client: &http.Client{
			Timeout: timeout,
			Transport: &http.Transport{
				MaxIdleConns:        4,
				IdleConnTimeout:     90 * time.Second,
				TLSHandshakeTimeout: timeout,
			},
		},
		maxBuf: maxBuf,
	}
}

func (s *Shipper) Stats() Stats {
	s.mu.Lock()
	defer s.mu.Unlock()
	st := s.stats
	st.Buffered = len(s.buf)
	return st
}

// Enqueue nimmt einen Batch an und versucht sofort zu senden. Schlägt das fehl,
// landet er im Puffer.
func (s *Shipper) Enqueue(ctx context.Context, b model.Batch) {
	if b.Len() == 0 {
		return
	}
	s.mu.Lock()
	s.buf = append(s.buf, b)
	for len(s.buf) > s.maxBuf {
		s.buf = s.buf[1:]
		s.stats.Dropped++
	}
	s.mu.Unlock()
	s.Flush(ctx)
}

// Flush schickt alles, was im Puffer liegt. Respektiert den Backoff.
func (s *Shipper) Flush(ctx context.Context) {
	s.mu.Lock()
	if time.Now().Before(s.nextTry) || len(s.buf) == 0 {
		s.mu.Unlock()
		return
	}
	pending := s.buf
	s.buf = nil
	s.mu.Unlock()

	var stillFailing []model.Batch
	for i, b := range pending {
		if err := s.send(ctx, b); err != nil {
			// Der Rest bleibt im Puffer, Reihenfolge bleibt erhalten.
			stillFailing = append(stillFailing, pending[i:]...)
			s.fail(err)
			break
		}
		s.ok()
	}

	if len(stillFailing) > 0 {
		s.mu.Lock()
		s.buf = append(stillFailing, s.buf...)
		for len(s.buf) > s.maxBuf {
			s.buf = s.buf[1:]
			s.stats.Dropped++
		}
		s.mu.Unlock()
	}
}

func (s *Shipper) ok() {
	s.mu.Lock()
	s.stats.Sent++
	s.stats.LastSuccess = time.Now()
	s.stats.LastError = ""
	s.backoff = 0
	s.nextTry = time.Time{}
	s.mu.Unlock()
}

func (s *Shipper) fail(err error) {
	s.mu.Lock()
	s.stats.Failed++
	s.stats.LastError = err.Error()
	if s.backoff == 0 {
		s.backoff = time.Second
	} else {
		s.backoff = time.Duration(math.Min(
			float64(s.backoff*2), float64(60*time.Second)))
	}
	// Jitter, damit nicht alle Agents gleichzeitig wiederkommen.
	jitter := time.Duration(float64(s.backoff) * 0.2 * randFloat())
	s.nextTry = time.Now().Add(s.backoff + jitter)
	s.mu.Unlock()
	log.Printf("aimon: Senden fehlgeschlagen (%v), nächster Versuch in %v", err, s.backoff)
}

func (s *Shipper) send(ctx context.Context, b model.Batch) error {
	return s.post(ctx, "/api/v1/ingest/batch", b)
}

// Post schickt beliebige Nutzlast an einen Ingest-Endpunkt.
func (s *Shipper) Post(ctx context.Context, path string, payload any) error {
	return s.post(ctx, path, payload)
}

func (s *Shipper) post(ctx context.Context, path string, payload any) error {
	raw, err := json.Marshal(payload)
	if err != nil {
		return fmt.Errorf("JSON: %w", err)
	}

	var body io.Reader = bytes.NewReader(raw)
	gzipped := false
	if len(raw) > 4096 {
		var buf bytes.Buffer
		zw := gzip.NewWriter(&buf)
		if _, err := zw.Write(raw); err == nil && zw.Close() == nil {
			body = bytes.NewReader(buf.Bytes())
			gzipped = true
		}
	}

	cctx, cancel := context.WithTimeout(ctx, s.Timeout)
	defer cancel()
	req, err := http.NewRequestWithContext(cctx, http.MethodPost, s.URL+path, body)
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", "Bearer "+s.Token)
	req.Header.Set("User-Agent", "aimon-agent")
	req.Header.Set("Idempotency-Key", randomKey())
	if gzipped {
		req.Header.Set("Content-Encoding", "gzip")
	}

	resp, err := s.Client.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	msg, _ := io.ReadAll(io.LimitReader(resp.Body, 4096))

	switch {
	case resp.StatusCode >= 200 && resp.StatusCode < 300:
		return nil
	case resp.StatusCode == 429:
		return fmt.Errorf("ratelimit (429)")
	case resp.StatusCode >= 400 && resp.StatusCode < 500:
		// 4xx heißt: das Schema passt nicht. Erneutes Senden ändert nichts,
		// also verwerfen statt den Puffer zu verstopfen.
		log.Printf("aimon: Batch verworfen, Server lehnt ab (%d): %s",
			resp.StatusCode, truncate(string(msg), 400))
		return nil
	default:
		return fmt.Errorf("HTTP %d: %s", resp.StatusCode, truncate(string(msg), 200))
	}
}

func truncate(s string, n int) string {
	s = strings.ReplaceAll(strings.TrimSpace(s), "\n", " ")
	if len(s) > n {
		return s[:n] + "…"
	}
	return s
}

func randomKey() string {
	var b [12]byte
	if _, err := rand.Read(b[:]); err != nil {
		return fmt.Sprintf("t%d", time.Now().UnixNano())
	}
	return hex.EncodeToString(b[:])
}

func randFloat() float64 {
	var b [8]byte
	if _, err := rand.Read(b[:]); err != nil {
		return 0.5
	}
	return float64(b[0]) / 255.0
}
