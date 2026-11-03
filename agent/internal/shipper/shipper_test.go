package shipper

import (
	"context"
	"net/http"
	"net/http/httptest"
	"sync/atomic"
	"testing"
	"time"

	"github.com/aimonitor/agent/internal/model"
)

func batch(n int) model.Batch {
	b := model.Batch{Node: model.Node{Name: "test"}}
	for i := 0; i < n; i++ {
		b.Samples = append(b.Samples, model.Sample{Metric: "x.y", Value: float64(i)})
	}
	return b
}

func TestSendsSuccessfully(t *testing.T) {
	var got atomic.Int64
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "Bearer tok" {
			t.Errorf("Token-Header fehlt: %q", r.Header.Get("Authorization"))
		}
		if r.Header.Get("Idempotency-Key") == "" {
			t.Error("Idempotency-Key fehlt")
		}
		got.Add(1)
		w.WriteHeader(202)
		w.Write([]byte(`{"accepted":1}`))
	}))
	defer srv.Close()

	s := New(srv.URL, "tok", 2*time.Second, 10)
	s.Enqueue(context.Background(), batch(3))
	if got.Load() != 1 {
		t.Errorf("Server-Aufrufe = %d", got.Load())
	}
	st := s.Stats()
	if st.Sent != 1 || st.Buffered != 0 {
		t.Errorf("Stats = %+v", st)
	}
}

func TestGzipAboveThreshold(t *testing.T) {
	var sawGzip atomic.Bool
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Content-Encoding") == "gzip" {
			sawGzip.Store(true)
		}
		w.WriteHeader(202)
	}))
	defer srv.Close()

	s := New(srv.URL, "tok", 2*time.Second, 10)
	s.Enqueue(context.Background(), batch(500)) // deutlich über 4 KB JSON
	if !sawGzip.Load() {
		t.Error("großer Batch sollte gzip-komprimiert gesendet werden")
	}
}

func TestBuffersOnServerErrorAndRetries(t *testing.T) {
	var fail atomic.Bool
	fail.Store(true)
	var calls atomic.Int64
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		if fail.Load() {
			w.WriteHeader(503)
			return
		}
		w.WriteHeader(202)
	}))
	defer srv.Close()

	s := New(srv.URL, "tok", time.Second, 10)
	s.Enqueue(context.Background(), batch(1))

	if st := s.Stats(); st.Buffered != 1 || st.Failed != 1 {
		t.Fatalf("nach Fehler erwartet buffered=1 failed=1, got %+v", st)
	}

	// Backoff verhindert sofortigen Retry.
	before := calls.Load()
	s.Flush(context.Background())
	if calls.Load() != before {
		t.Error("Flush während Backoff hätte nicht senden dürfen")
	}

	// Backoff überspringen und Server reparieren.
	fail.Store(false)
	s.mu.Lock()
	s.nextTry = time.Time{}
	s.mu.Unlock()
	s.Flush(context.Background())

	if st := s.Stats(); st.Buffered != 0 || st.Sent != 1 {
		t.Errorf("nach Erholung erwartet buffered=0 sent=1, got %+v", st)
	}
}

func TestDropsOldestWhenBufferFull(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(503)
	}))
	defer srv.Close()

	s := New(srv.URL, "tok", 500*time.Millisecond, 3)
	for i := 0; i < 10; i++ {
		s.Enqueue(context.Background(), batch(1))
	}
	st := s.Stats()
	if st.Buffered > 3 {
		t.Errorf("Puffer = %d, Obergrenze 3", st.Buffered)
	}
	if st.Dropped == 0 {
		t.Error("ältere Batches hätten verworfen werden müssen")
	}
}

func TestDiscards4xxWithoutRetry(t *testing.T) {
	var calls atomic.Int64
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		w.WriteHeader(400)
		w.Write([]byte(`{"detail":{"errors":["/samples/0/metric: ungültig"]}}`))
	}))
	defer srv.Close()

	s := New(srv.URL, "tok", time.Second, 10)
	s.Enqueue(context.Background(), batch(1))

	st := s.Stats()
	// 400 heißt Schemafehler: erneutes Senden ändert nichts, also verwerfen
	// statt den Puffer dauerhaft zu blockieren.
	if st.Buffered != 0 {
		t.Errorf("4xx-Batch sollte verworfen werden, Puffer = %d", st.Buffered)
	}
	if st.Failed != 0 {
		t.Errorf("4xx darf nicht als Sendefehler zählen, failed = %d", st.Failed)
	}
	if calls.Load() != 1 {
		t.Errorf("erwartet genau 1 Versuch, got %d", calls.Load())
	}
}

func TestServerDownDoesNotBlock(t *testing.T) {
	s := New("http://127.0.0.1:1", "tok", 300*time.Millisecond, 5)
	start := time.Now()
	for i := 0; i < 3; i++ {
		s.Enqueue(context.Background(), batch(1))
	}
	// Erster Versuch läuft in den Verbindungsfehler, danach greift der Backoff.
	if d := time.Since(start); d > 3*time.Second {
		t.Errorf("Enqueue blockierte %v - der Agent darf den Host nicht aufhalten", d)
	}
	if s.Stats().Buffered == 0 {
		t.Error("bei unerreichbarem Server muss gepuffert werden")
	}
}

func TestEmptyBatchIsNoop(t *testing.T) {
	var calls atomic.Int64
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		w.WriteHeader(202)
	}))
	defer srv.Close()
	s := New(srv.URL, "tok", time.Second, 5)
	s.Enqueue(context.Background(), model.Batch{Node: model.Node{Name: "n"}})
	if calls.Load() != 0 {
		t.Error("leerer Batch sollte nicht gesendet werden")
	}
}
