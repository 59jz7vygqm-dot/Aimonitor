package collector

import (
	"context"
	"encoding/csv"
	"fmt"
	"os/exec"
	"strconv"
	"strings"
	"time"

	"github.com/aimonitor/agent/internal/model"
)

// GPU-Metriken über nvidia-smi.
//
// Bewusst nicht NVML: NVML über cgo würde libnvidia-ml.so zur Buildzeit
// verlangen und die statische Binary zerstören. nvidia-smi ist überall da, wo
// der Treiber installiert ist, und das CSV-Format ist stabil.
//
// Ein Feld, das der Treiber nicht liefert ("N/A", "[Not Supported]"), wird
// übersprungen statt als 0 gemeldet - eine 0 wäre eine Falschaussage.

var gpuQueryFields = []string{
	"index", "uuid", "name",
	"utilization.gpu", "utilization.memory",
	"memory.total", "memory.used", "memory.reserved",
	"temperature.gpu", "temperature.memory",
	"power.draw", "power.limit", "enforced.power.limit",
	"fan.speed",
	"clocks.current.sm", "clocks.current.memory", "clocks.max.sm",
	"clocks_throttle_reasons.hw_slowdown",
	"clocks_throttle_reasons.hw_thermal_slowdown",
	"clocks_throttle_reasons.sw_thermal_slowdown",
	"clocks_throttle_reasons.sw_power_cap",
	"pcie.link.gen.current", "pcie.link.width.current",
	"ecc.errors.uncorrected.volatile.total",
	"persistence_mode", "compute_mode",
}

type GPUCollector struct {
	Binary string
	// FailCount zählt aufeinanderfolgende Fehlschläge, damit der Agent nicht
	// bei jedem Intervall dieselbe Fehlermeldung schickt.
	failCount int
	absent    bool
	// memWarned verhindert, dass dieselbe Erklärung bei jedem Scrape erneut
	// als Ereignis geschickt wird.
	memWarned bool
}

func NewGPUCollector() *GPUCollector {
	bin := "nvidia-smi"
	if p, err := exec.LookPath(bin); err == nil {
		bin = p
	}
	return &GPUCollector{Binary: bin}
}

func (g *GPUCollector) Name() string { return "gpu" }

func (g *GPUCollector) Collect(ctx context.Context, now float64) (model.Batch, error) {
	var b model.Batch
	if g.absent {
		return b, nil
	}

	args := []string{
		"--query-gpu=" + strings.Join(gpuQueryFields, ","),
		"--format=csv,noheader,nounits",
	}
	out, err := runCmd(ctx, g.Binary, args...)
	if err != nil {
		g.failCount++
		if g.failCount == 1 || g.failCount == 10 {
			if strings.Contains(err.Error(), "executable file not found") ||
				strings.Contains(err.Error(), "no such file") {
				// Kein NVIDIA-Treiber: dauerhaft abschalten, kein Rauschen.
				g.absent = true
				return b, nil
			}
			b.Events = append(b.Events, model.Event{
				Kind:     "agent.collector_failed",
				Severity: "warning",
				Title:    "nvidia-smi nicht auswertbar",
				TS:       now,
				Detail:   map[string]any{"collector": "gpu", "error": err.Error()},
				DedupKey: "agent.collector_failed:gpu",
			})
		}
		b.Samples = append(b.Samples, model.Sample{
			Metric: "host.agent_scrape_errors_total", Value: float64(g.failCount),
			TS: now, Labels: map[string]string{"collector": "gpu"},
		})
		return b, nil
	}
	g.failCount = 0

	rows, err := csv.NewReader(strings.NewReader(out)).ReadAll()
	if err != nil {
		return b, fmt.Errorf("nvidia-smi CSV: %w", err)
	}

	// Liefert --query-gpu keinen Speicher (gemeinsamer Speicher auf
	// Grace-Blackwell-Systemen), die ausführliche Ausgabe nachladen. Einmal
	// pro Scrape, nicht pro GPU.
	var memFallback map[int]GPUMemory
	for _, row := range rows {
		if len(row) >= len(gpuQueryFields) && !valid(row[5]) { // memory.total
			memFallback = g.queryMemory(ctx)
			break
		}
	}

	count := 0
	for _, row := range rows {
		if len(row) < len(gpuQueryFields) {
			continue
		}
		f := make(map[string]string, len(row))
		for i, name := range gpuQueryFields {
			f[name] = strings.TrimSpace(row[i])
		}
		idx := f["index"]
		if idx == "" {
			continue
		}
		count++
		lbl := map[string]string{"gpu": idx}
		if v := f["uuid"]; valid(v) {
			lbl["uuid"] = v
		}
		if v := f["name"]; valid(v) {
			lbl["name"] = v
		}

		add := func(metric, field string, scale float64) {
			if v, ok := num(f[field]); ok {
				b.Samples = append(b.Samples, model.Sample{
					Metric: metric, Value: v * scale, TS: now, Labels: lbl,
				})
			}
		}
		const mib = 1024 * 1024

		add("gpu.util_pct", "utilization.gpu", 1)
		add("gpu.mem_util_pct", "utilization.memory", 1)

		if _, ok := num(f["memory.total"]); ok {
			add("gpu.mem_total_bytes", "memory.total", mib)
			add("gpu.mem_used_bytes", "memory.used", mib)
			add("gpu.mem_reserved_bytes", "memory.reserved", mib)
		} else if fb, ok := memFallback[gpuIndex(idx)]; ok && fb.HasTotal {
			// Zahlen aus `nvidia-smi -q`. Das Label sagt, woher sie kommen -
			// auf Systemen mit gemeinsamem Speicher bedeutet "frei" etwas
			// anderes als bei einer Karte mit eigenem VRAM.
			ulbl := map[string]string{}
			for k, v := range lbl {
				ulbl[k] = v
			}
			ulbl["memory"] = "unified"
			b.Samples = append(b.Samples,
				model.Sample{Metric: "gpu.mem_total_bytes",
					Value: fb.TotalMiB * mib, TS: now, Labels: ulbl},
			)
			if fb.HasUsed {
				b.Samples = append(b.Samples,
					model.Sample{Metric: "gpu.mem_used_bytes",
						Value: fb.UsedMiB * mib, TS: now, Labels: ulbl},
				)
			}
			if fb.ReservedMiB > 0 {
				b.Samples = append(b.Samples,
					model.Sample{Metric: "gpu.mem_reserved_bytes",
						Value: fb.ReservedMiB * mib, TS: now, Labels: ulbl},
				)
			}
			b.Samples = append(b.Samples,
				model.Sample{Metric: "gpu.mem_unified", Value: 1, TS: now,
					Labels: lbl},
			)
		} else {
			// Keine der beiden Quellen liefert etwas. Das ausdrücklich
			// melden, statt stillschweigend eine Lücke zu lassen - sonst
			// sieht es im Dashboard aus wie ein Ausfall des Agents.
			b.Samples = append(b.Samples,
				model.Sample{Metric: "gpu.mem_unavailable", Value: 1, TS: now,
					Labels: lbl},
			)
			if !g.memWarned {
				g.memWarned = true
				b.Events = append(b.Events, model.Event{
					Kind:     "gpu.memory_unavailable",
					Severity: "info",
					Title:    "Der Treiber meldet für diese GPU keinen Speicher",
					TS:       now,
					Detail: map[string]any{
						"gpu":  idx,
						"name": f["name"],
						"hinweis": "Weder --query-gpu=memory.total noch " +
							"'nvidia-smi -q -d MEMORY' liefern Zahlen. Auf Systemen " +
							"mit gemeinsamem Speicher ist der Arbeitsspeicher des " +
							"Hosts die eigentliche Grenze - siehe host.mem_used_bytes. " +
							"Rohausgabe zum Nachreichen: 'aimon-agent gpudump'.",
					},
					DedupKey: "gpu.memory_unavailable:" + idx,
				})
			}
		}
		add("gpu.temp_c", "temperature.gpu", 1)
		add("gpu.temp_mem_c", "temperature.memory", 1)
		add("gpu.power_w", "power.draw", 1)
		if _, ok := num(f["enforced.power.limit"]); ok {
			add("gpu.power_limit_w", "enforced.power.limit", 1)
		} else {
			add("gpu.power_limit_w", "power.limit", 1)
		}
		add("gpu.fan_pct", "fan.speed", 1)
		add("gpu.clock_sm_mhz", "clocks.current.sm", 1)
		add("gpu.clock_mem_mhz", "clocks.current.memory", 1)
		add("gpu.clock_sm_max_mhz", "clocks.max.sm", 1)
		add("gpu.pcie_gen", "pcie.link.gen.current", 1)
		add("gpu.pcie_width", "pcie.link.width.current", 1)
		add("gpu.ecc_errors_total", "ecc.errors.uncorrected.volatile.total", 1)

		thr := func(metric string, fields ...string) {
			any := false
			active := false
			for _, fl := range fields {
				v := strings.ToLower(f[fl])
				if !valid(v) {
					continue
				}
				any = true
				if v == "active" || v == "1" {
					active = true
				}
			}
			if any {
				val := 0.0
				if active {
					val = 1.0
				}
				b.Samples = append(b.Samples, model.Sample{
					Metric: metric, Value: val, TS: now, Labels: lbl,
				})
			}
		}
		thr("gpu.throttle_hw", "clocks_throttle_reasons.hw_slowdown")
		thr("gpu.throttle_thermal",
			"clocks_throttle_reasons.hw_thermal_slowdown",
			"clocks_throttle_reasons.sw_thermal_slowdown")
		thr("gpu.throttle_power", "clocks_throttle_reasons.sw_power_cap")
	}

	b.Samples = append(b.Samples, model.Sample{
		Metric: "gpu.count", Value: float64(count), TS: now,
	})

	// Prozesse auf der GPU: zeigt, ob vLLM und ComfyUI sich eine Karte teilen.
	if procOut, err := runCmd(ctx, g.Binary,
		"--query-compute-apps=gpu_uuid,pid,process_name,used_gpu_memory",
		"--format=csv,noheader,nounits"); err == nil {
		prows, _ := csv.NewReader(strings.NewReader(procOut)).ReadAll()
		for _, row := range prows {
			if len(row) < 4 {
				continue
			}
			mem, ok := num(strings.TrimSpace(row[3]))
			if !ok {
				continue
			}
			name := strings.TrimSpace(row[2])
			if i := strings.LastIndexByte(name, '/'); i >= 0 {
				name = name[i+1:]
			}
			b.Samples = append(b.Samples, model.Sample{
				Metric: "gpu.proc_mem_bytes", Value: mem * 1024 * 1024, TS: now,
				Labels: map[string]string{
					"uuid":  strings.TrimSpace(row[0]),
					"pid":   strings.TrimSpace(row[1]),
					"pname": name,
				},
			})
		}
	}

	return b, nil
}

func runCmd(ctx context.Context, bin string, args ...string) (string, error) {
	cctx, cancel := context.WithTimeout(ctx, 8*time.Second)
	defer cancel()
	cmd := exec.CommandContext(cctx, bin, args...)
	out, err := cmd.Output()
	if err != nil {
		return "", fmt.Errorf("%s: %w", bin, err)
	}
	return string(out), nil
}

// valid filtert die Platzhalter, die nvidia-smi für nicht unterstützte Felder liefert.
func valid(s string) bool {
	switch strings.ToLower(strings.TrimSpace(s)) {
	case "", "n/a", "[n/a]", "not supported", "[not supported]",
		"unknown error", "[unknown error]", "insufficient permissions",
		"[insufficient permissions]":
		return false
	}
	return true
}

func num(s string) (float64, bool) {
	s = strings.TrimSpace(s)
	if !valid(s) {
		return 0, false
	}
	v, err := strconv.ParseFloat(s, 64)
	if err != nil {
		return 0, false
	}
	return v, true
}

// queryMemory holt die Speicherwerte aus der ausführlichen Ausgabe.
// Fehlschläge sind kein Fehler: dann bleibt es eben bei der Lücke.
func (g *GPUCollector) queryMemory(ctx context.Context) map[int]GPUMemory {
	out, err := runCmd(ctx, g.Binary, "-q", "-d", "MEMORY")
	if err != nil {
		return nil
	}
	res := map[int]GPUMemory{}
	for _, m := range ParseNvidiaSmiMemory(out) {
		res[m.Index] = m
	}
	return res
}

func gpuIndex(s string) int {
	n, err := strconv.Atoi(strings.TrimSpace(s))
	if err != nil {
		return -1
	}
	return n
}

// RunNvidiaSmi und GPUQueryArgs sind für den Diagnosebefehl `aimon-agent
// gpudump` nach außen gelegt: der soll genau das zeigen, was der Collector
// auch benutzt, und nicht eine zweite, möglicherweise abweichende Fassung.
func RunNvidiaSmi(ctx context.Context, bin string, args ...string) (string, error) {
	return runCmd(ctx, bin, args...)
}

func GPUQueryArgs() []string {
	return []string{
		"--query-gpu=" + strings.Join(gpuQueryFields, ","),
		"--format=csv,noheader,nounits",
	}
}
