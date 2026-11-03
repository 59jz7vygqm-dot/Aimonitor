// aimon-agent sammelt Metriken auf einem Host und schickt sie an den aimon-server.
//
//	aimon-agent -config /etc/aimon/agent.yaml
//	aimon-agent tap -upstream http://127.0.0.1:8000 -listen :9500
//	aimon-agent check          # Konfiguration und Erreichbarkeit prüfen
//	aimon-agent discover       # lokal gefundene Dienste anzeigen
package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"log"
	"net/http"
	"os"
	"os/signal"
	"strings"
	"sync"
	"syscall"
	"time"

	"github.com/aimonitor/agent/internal/collector"
	"github.com/aimonitor/agent/internal/config"
	"github.com/aimonitor/agent/internal/model"
	"github.com/aimonitor/agent/internal/shipper"
	"github.com/aimonitor/agent/internal/tap"
)

var version = "0.1.0"

type collectorIface interface {
	Name() string
	Collect(ctx context.Context, now float64) (model.Batch, error)
}

func main() {
	log.SetFlags(log.LstdFlags)
	log.SetPrefix("aimon-agent: ")

	if len(os.Args) > 1 && !strings.HasPrefix(os.Args[1], "-") {
		cmd := os.Args[1]
		os.Args = append(os.Args[:1], os.Args[2:]...)
		switch cmd {
		case "tap":
			runTap()
			return
		case "check":
			runCheck()
			return
		case "discover":
			runDiscover()
			return
		case "version":
			fmt.Println(version)
			return
		default:
			log.Fatalf("unbekanntes Kommando %q (tap|check|discover|version)", cmd)
		}
	}
	runAgent()
}

func loadConfig() config.Config {
	var path string
	flag.StringVar(&path, "config", envOr("AIMON_CONFIG", "/etc/aimon/agent.yaml"),
		"Pfad zur Konfiguration")
	flag.Parse()

	c, err := config.Load(path)
	if err != nil {
		log.Fatalf("Konfiguration: %v", err)
	}
	return c
}

func envOr(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}

// ------------------------------------------------------------- Hauptbetrieb

func runAgent() {
	c := loadConfig()
	log.Printf("Version %s, Node %q, Server %s, Intervall %v",
		version, c.NodeName, c.ServerURL, c.Interval)

	ctx, stop := signal.NotifyContext(context.Background(),
		syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	ship := shipper.New(c.ServerURL, c.Token, c.Timeout, c.BufferBatches)
	httpClient := &http.Client{Timeout: c.Timeout}

	targets := c.Targets
	if c.DiscoveryEnabled {
		found := collector.Discover(ctx, c.NodeName, c.Timeout)
		if len(found) > 0 {
			names := make([]string, 0, len(found))
			for _, f := range found {
				names = append(names, f.Kind+" "+f.URL)
			}
			log.Printf("Discovery: %s", strings.Join(names, ", "))
		}
		targets = collector.MergeTargets(c.Targets, found)
	}
	if len(targets) == 0 {
		log.Printf("Hinweis: keine Dienste konfiguriert oder gefunden – " +
			"es werden nur Host- und GPU-Metriken gemeldet")
	}

	var fast []collectorIface
	if c.GPUEnabled {
		fast = append(fast, collector.NewGPUCollector())
	}
	if c.HostEnabled {
		fast = append(fast, collector.NewHostCollector())
	}

	var svcCollectors []*collector.ServiceCollector
	for _, t := range targets {
		svcCollectors = append(svcCollectors, &collector.ServiceCollector{
			Target: t, HTTP: httpClient, Node: c.NodeName,
		})
	}

	var slow []collectorIface
	if len(c.Peers) > 0 {
		slow = append(slow, &collector.PeerProbe{
			Targets: c.Peers, HTTP: httpClient, Timeout: c.Timeout,
		})
	}

	node := model.Node{
		Name:         c.NodeName,
		Hostname:     c.Hostname,
		AgentVersion: version,
		Labels:       c.Labels,
	}

	if c.ListenAddr != "" {
		go serveDebug(c.ListenAddr, ship, &node, targets)
	}

	tick := time.NewTicker(c.Interval)
	defer tick.Stop()
	probeTick := time.NewTicker(c.ProbeInterval)
	defer probeTick.Stop()

	scrape := func(includeSlow bool) {
		now := float64(time.Now().UnixNano()) / 1e9
		batch := model.Batch{Node: node}

		all := make([]collectorIface, 0, len(fast)+len(svcCollectors)+len(slow))
		all = append(all, fast...)
		for _, sc := range svcCollectors {
			all = append(all, sc)
		}
		if includeSlow {
			all = append(all, slow...)
		}

		var mu sync.Mutex
		var wg sync.WaitGroup
		for _, col := range all {
			wg.Add(1)
			go func(col collectorIface) {
				defer wg.Done()
				start := time.Now()
				cctx, cancel := context.WithTimeout(ctx, c.Interval+5*time.Second)
				defer cancel()
				b, err := col.Collect(cctx, now)
				dur := float64(time.Since(start).Microseconds()) / 1000.0
				mu.Lock()
				defer mu.Unlock()
				if err != nil {
					log.Printf("Collector %s: %v", col.Name(), err)
					batch.Samples = append(batch.Samples, model.Sample{
						Metric: "host.agent_scrape_errors_total", Value: 1, TS: now,
						Labels: map[string]string{"collector": col.Name()},
					})
					return
				}
				batch.Merge(b)
				batch.Samples = append(batch.Samples, model.Sample{
					Metric: "host.agent_scrape_ms", Value: dur, TS: now,
					Labels: map[string]string{"collector": col.Name()},
				})
			}(col)
		}
		wg.Wait()

		if includeSlow && c.OllamaProbe {
			for _, sc := range svcCollectors {
				if sc.Target.Kind != "ollama" {
					continue
				}
				for _, m := range sc.Target.Models {
					pctx, cancel := context.WithTimeout(ctx, 30*time.Second)
					batch.Merge(sc.ProbeOllamaTTFT(pctx, now, m))
					cancel()
				}
			}
		}

		st := ship.Stats()
		batch.Samples = append(batch.Samples,
			model.Sample{Metric: "agent.buffered_batches", Value: float64(st.Buffered),
				TS: now},
			model.Sample{Metric: "agent.dropped_batches_total", Value: float64(st.Dropped),
				TS: now},
			model.Sample{Metric: "agent.send_failures_total", Value: float64(st.Failed),
				TS: now},
		)

		ship.Enqueue(ctx, batch)
	}

	scrape(true)
	for {
		select {
		case <-ctx.Done():
			log.Printf("beende, %d Batches im Puffer", ship.Stats().Buffered)
			fctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
			ship.Flush(fctx)
			cancel()
			return
		case <-probeTick.C:
			scrape(true)
		case <-tick.C:
			scrape(false)
		}
	}
}

func serveDebug(addr string, ship *shipper.Shipper, node *model.Node, targets []config.Target) {
	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, r *http.Request) {
		st := ship.Stats()
		code := http.StatusOK
		if st.LastError != "" && time.Since(st.LastSuccess) > 5*time.Minute {
			code = http.StatusServiceUnavailable
		}
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(code)
		json.NewEncoder(w).Encode(map[string]any{
			"version": version, "node": node.Name,
			"sent": st.Sent, "failed": st.Failed, "dropped": st.Dropped,
			"buffered": st.Buffered, "last_error": st.LastError,
			"last_success": st.LastSuccess,
			"targets":      targetKeys(targets),
		})
	})
	log.Printf("Debug-Endpunkt auf %s", addr)
	srv := &http.Server{Addr: addr, Handler: mux, ReadHeaderTimeout: 5 * time.Second}
	if err := srv.ListenAndServe(); err != nil {
		log.Printf("Debug-Endpunkt: %v", err)
	}
}

func targetKeys(ts []config.Target) []string {
	out := make([]string, 0, len(ts))
	for _, t := range ts {
		out = append(out, t.Key)
	}
	return out
}

// ----------------------------------------------------------------- Tap-Modus

func runTap() {
	var cfgPath, listen, upstream, service, node string
	flag.StringVar(&cfgPath, "config", envOr("AIMON_CONFIG", "/etc/aimon/agent.yaml"), "Konfiguration")
	flag.StringVar(&listen, "listen", "", "Adresse, auf der der Tap lauscht")
	flag.StringVar(&upstream, "upstream", "", "Backend-URL, z. B. http://127.0.0.1:8000")
	flag.StringVar(&service, "service", "", "Service-Key des Backends")
	flag.StringVar(&node, "node", "", "Node-Name")
	flag.Parse()

	c, err := config.Load(cfgPath)
	if err != nil {
		log.Fatalf("Konfiguration: %v", err)
	}
	if listen != "" {
		c.TapListen = listen
	}
	if upstream != "" {
		c.TapUpstream = upstream
	}
	if service != "" {
		c.TapService = service
	}
	if node != "" {
		c.NodeName = node
	}
	if c.TapUpstream == "" {
		log.Fatal("kein -upstream: der Tap braucht ein Backend, das er weiterreicht")
	}
	if c.TapService == "" {
		c.TapService = config.DeriveKey(c.TapKind, c.TapUpstream, c.NodeName)
		log.Printf("kein -service angegeben, abgeleitet: %s", c.TapService)
	}

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	ship := shipper.New(c.ServerURL, c.Token, c.Timeout, c.BufferBatches)
	t, err := tap.New(c.TapUpstream, c.TapService, c.NodeName, ship)
	if err != nil {
		log.Fatalf("Tap: %v", err)
	}

	// Sich selbst als Dienst registrieren, damit die Topologie den Messpunkt zeigt.
	tapKey := config.DeriveKey("tap", "http://"+c.TapListen, c.NodeName)
	ship.Enqueue(ctx, model.Batch{
		Node: model.Node{Name: c.NodeName, AgentVersion: version},
		Services: []model.Service{{
			Key: tapKey, Kind: "tap", Name: "aimon-tap",
			BaseURL: "http://" + c.TapListen, Version: version,
			Labels: map[string]string{"upstream": c.TapUpstream,
				"measures": c.TapService},
		}},
	})

	go func() {
		tk := time.NewTicker(5 * time.Second)
		defer tk.Stop()
		for {
			select {
			case <-ctx.Done():
				t.Flush(context.Background())
				return
			case <-tk.C:
				t.Flush(ctx)
				ship.Flush(ctx)
			}
		}
	}()

	srv := &http.Server{
		Addr:              c.TapListen,
		Handler:           t,
		ReadHeaderTimeout: 10 * time.Second,
		// Kein WriteTimeout: eine lange Streaming-Antwort darf nicht abgeschnitten
		// werden, nur weil sie dauert.
	}
	go func() {
		<-ctx.Done()
		sctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		srv.Shutdown(sctx)
	}()

	log.Printf("Tap %s -> %s (meldet als %s)", c.TapListen, c.TapUpstream, c.TapService)
	if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
		log.Fatalf("Tap-Server: %v", err)
	}
	t.Flush(context.Background())
	fctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	ship.Flush(fctx)
	cancel()
}

// ----------------------------------------------------------- Diagnosemodi

func runCheck() {
	c := loadConfig()
	fmt.Printf("Konfiguration gelesen.\n")
	fmt.Printf("  Node:     %s\n", c.NodeName)
	fmt.Printf("  Server:   %s\n", c.ServerURL)
	fmt.Printf("  Token:    %s… (%d Zeichen)\n", safePrefix(c.Token), len(c.Token))
	fmt.Printf("  Intervall: %v / Probes: %v\n", c.Interval, c.ProbeInterval)
	fmt.Printf("  GPU: %v  Host: %v  Discovery: %v\n",
		c.GPUEnabled, c.HostEnabled, c.DiscoveryEnabled)

	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()

	fmt.Printf("\nToken gegen %s prüfen …\n", c.ServerURL)
	req, _ := http.NewRequestWithContext(ctx, http.MethodGet,
		strings.TrimRight(c.ServerURL, "/")+"/api/v1/ingest/echo", nil)
	req.Header.Set("Authorization", "Bearer "+c.Token)
	resp, err := (&http.Client{Timeout: c.Timeout}).Do(req)
	if err != nil {
		fmt.Printf("  FEHLER: %v\n", err)
		os.Exit(1)
	}
	defer resp.Body.Close()
	var echo map[string]any
	json.NewDecoder(resp.Body).Decode(&echo)
	if resp.StatusCode != 200 {
		fmt.Printf("  FEHLER: HTTP %d %v\n", resp.StatusCode, echo)
		os.Exit(1)
	}
	fmt.Printf("  OK: Token %q, Scopes %v\n", echo["token"], echo["scopes"])

	fmt.Printf("\nGPU-Collector …\n")
	g := collector.NewGPUCollector()
	gb, _ := g.Collect(ctx, float64(time.Now().Unix()))
	fmt.Printf("  %d Samples (nvidia-smi: %s)\n", len(gb.Samples), g.Binary)

	fmt.Printf("\nHost-Collector …\n")
	h := collector.NewHostCollector()
	h.Collect(ctx, float64(time.Now().Unix()))
	time.Sleep(time.Second)
	hb, _ := h.Collect(ctx, float64(time.Now().Unix()))
	fmt.Printf("  %d Samples\n", len(hb.Samples))

	targets := c.Targets
	if c.DiscoveryEnabled {
		targets = collector.MergeTargets(c.Targets,
			collector.Discover(ctx, c.NodeName, c.Timeout))
	}
	fmt.Printf("\n%d Dienste:\n", len(targets))
	httpClient := &http.Client{Timeout: c.Timeout}
	for _, t := range targets {
		sc := &collector.ServiceCollector{Target: t, HTTP: httpClient, Node: c.NodeName}
		b, _ := sc.Collect(ctx, float64(time.Now().Unix()))
		state := "nicht erreichbar"
		if len(b.Probes) > 0 && b.Probes[0].OK {
			state = fmt.Sprintf("ok, %d Samples", len(b.Samples))
		} else if len(b.Probes) > 0 && b.Probes[0].Error != "" {
			state = b.Probes[0].Error
		}
		fmt.Printf("  %-12s %-34s %s\n", t.Kind, t.URL, state)
	}

	if len(c.Peers) > 0 {
		fmt.Printf("\n%d Peer-Probes:\n", len(c.Peers))
		pp := &collector.PeerProbe{Targets: c.Peers, HTTP: httpClient, Timeout: c.Timeout}
		b, _ := pp.Collect(ctx, float64(time.Now().Unix()))
		for _, pr := range b.Probes {
			if pr.OK {
				fmt.Printf("  ok      %-40s %.1f ms\n", pr.Target, *pr.LatencyMs)
			} else {
				fmt.Printf("  FEHLER  %-40s %s\n", pr.Target, pr.Error)
			}
		}
	}
	fmt.Printf("\nPrüfung abgeschlossen.\n")
}

func runDiscover() {
	var nodeName string
	flag.StringVar(&nodeName, "node", "localhost", "Node-Name für die Key-Bildung")
	flag.Parse()
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	found := collector.Discover(ctx, nodeName, 3*time.Second)
	if len(found) == 0 {
		fmt.Println("Keine Dienste gefunden.")
		fmt.Println("Gesucht wurde auf 127.0.0.1 nach vLLM (8000/8001/8080), Ollama (11434),")
		fmt.Println("ComfyUI (8188), OpenWebUI (3000/8080) und SearXNG (8888/8080).")
		fmt.Println("Läuft etwas auf einem anderen Port, gehört es in targets: in die Konfiguration.")
		return
	}
	fmt.Println("# Für /etc/aimon/agent.yaml:")
	fmt.Println("targets:")
	for _, t := range found {
		fmt.Printf("  - kind: %s\n    url: %s\n", t.Kind, t.URL)
	}
}

func safePrefix(s string) string {
	if len(s) <= 8 {
		return "***"
	}
	return s[:8]
}
