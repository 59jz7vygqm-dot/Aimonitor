package collector

import (
	"context"
	"os"
	"path/filepath"
	"testing"
	"time"
)

// fakeSMI legt ein ausführbares Skript an, das sich wie nvidia-smi verhält.
func fakeSMI(t *testing.T, body string) string {
	t.Helper()
	p := filepath.Join(t.TempDir(), "nvidia-smi")
	if err := os.WriteFile(p, []byte("#!/bin/sh\n"+body), 0o755); err != nil {
		t.Fatal(err)
	}
	return p
}

type sample struct {
	Value  float64
	Labels map[string]string
}

func collect(t *testing.T, bin string) (map[string]sample, []string) {
	t.Helper()
	g := NewGPUCollector()
	g.Binary = bin
	b, err := g.Collect(context.Background(), float64(time.Now().Unix()))
	if err != nil {
		t.Fatalf("Collect: %v", err)
	}
	out := map[string]sample{}
	for _, s := range b.Samples {
		out[s.Metric] = sample{Value: s.Value, Labels: s.Labels}
	}
	var events []string
	for _, e := range b.Events {
		events = append(events, e.Kind)
	}
	return out, events
}

// Karte mit eigenem VRAM: --query-gpu liefert alles.
const normalSMI = `
case "$*" in
  *--query-compute-apps*) exit 0 ;;
  *"-q -d MEMORY"*) echo "sollte nicht aufgerufen werden" ; exit 1 ;;
  *--query-gpu*)
    echo "0, GPU-xyz, NVIDIA L4, 100, 92, 23034, 21100, 512, 59, [N/A], 70.12, 72.00, 72.00, [N/A], 1800, 6250, 2040, Not Active, Not Active, Not Active, Active, 4, 8, 0, Enabled, Default"
    ;;
esac
`

// Gemeinsamer Speicher: --query-gpu meldet N/A, -q -d MEMORY hat die Zahlen.
const unifiedSMI = `
case "$*" in
  *--query-compute-apps*) exit 0 ;;
  *"-q -d MEMORY"*)
    printf '%s\n' \
      "Attached GPUs                             : 1" \
      "GPU 00000009:01:00.0" \
      "    FB Memory Usage" \
      "        Total                             : 119808 MiB" \
      "        Reserved                          : 2048 MiB" \
      "        Used                              : 3200 MiB" \
      "        Free                              : 114560 MiB" \
      "    BAR1 Memory Usage" \
      "        Total                             : N/A"
    ;;
  *--query-gpu*)
    echo "0, GPU-abc, NVIDIA GB10, 0, [N/A], [N/A], [N/A], [N/A], 36, [N/A], 3.21, 72.00, [N/A], [N/A], 1200, [N/A], [N/A], Not Active, Not Active, Not Active, Active, 5, 8, 0, Enabled, Default"
    ;;
esac
`

// Gar keine Speicherwerte, auch nicht im Rückfallweg.
const noMemorySMI = `
case "$*" in
  *--query-compute-apps*) exit 0 ;;
  *"-q -d MEMORY"*)
    printf '%s\n' \
      "Attached GPUs                             : 1" \
      "GPU 00000009:01:00.0" \
      "    FB Memory Usage" \
      "        Total                             : N/A"
    ;;
  *--query-gpu*)
    echo "0, GPU-abc, NVIDIA GB10, 0, [N/A], [N/A], [N/A], [N/A], 36, [N/A], 3.21, 72.00, [N/A], [N/A], 1200, [N/A], [N/A], Not Active, Not Active, Not Active, Active, 5, 8, 0, Enabled, Default"
    ;;
esac
`

func TestGPUNormaleKarte(t *testing.T) {
	m, _ := collect(t, fakeSMI(t, normalSMI))

	const mib = 1024 * 1024
	if m["gpu.mem_total_bytes"].Value != 23034*mib {
		t.Errorf("mem_total = %v", m["gpu.mem_total_bytes"].Value)
	}
	if m["gpu.mem_used_bytes"].Value != 21100*mib {
		t.Errorf("mem_used = %v", m["gpu.mem_used_bytes"].Value)
	}
	if m["gpu.temp_c"].Value != 59 {
		t.Errorf("temp = %v", m["gpu.temp_c"].Value)
	}
	if m["gpu.util_pct"].Value != 100 {
		t.Errorf("util = %v", m["gpu.util_pct"].Value)
	}
	// enforced.power.limit hat Vorrang vor power.limit
	if m["gpu.power_limit_w"].Value != 72 {
		t.Errorf("power_limit = %v", m["gpu.power_limit_w"].Value)
	}
	if _, ok := m["gpu.mem_unified"]; ok {
		t.Error("eine gewöhnliche Karte darf nicht als unified markiert werden")
	}
	if _, ok := m["gpu.mem_unavailable"]; ok {
		t.Error("Speicher war vorhanden, trotzdem als fehlend markiert")
	}
	// Temperatur-Sensor nicht vorhanden -> Metrik fehlt, statt 0 zu behaupten
	if _, ok := m["gpu.temp_mem_c"]; ok {
		t.Error("nicht gemeldeter Sensor hätte weggelassen werden müssen")
	}
}

func TestGPUGemeinsamerSpeicher(t *testing.T) {
	m, _ := collect(t, fakeSMI(t, unifiedSMI))

	const mib = 1024 * 1024
	if m["gpu.mem_total_bytes"].Value != 119808*mib {
		t.Fatalf("mem_total = %v – der Rückfallweg hat nicht gegriffen",
			m["gpu.mem_total_bytes"].Value)
	}
	if m["gpu.mem_used_bytes"].Value != 3200*mib {
		t.Errorf("mem_used = %v", m["gpu.mem_used_bytes"].Value)
	}
	// Die Herkunft muss am Label erkennbar sein: "frei" bedeutet bei
	// gemeinsamem Speicher etwas anderes als bei eigenem VRAM.
	if m["gpu.mem_total_bytes"].Labels["memory"] != "unified" {
		t.Errorf("Label fehlt: %v", m["gpu.mem_total_bytes"].Labels)
	}
	if m["gpu.mem_unified"].Value != 1 {
		t.Error("gpu.mem_unified fehlt")
	}
	// Die übrigen Werte kommen weiter aus --query-gpu
	if m["gpu.temp_c"].Value != 36 {
		t.Errorf("temp = %v", m["gpu.temp_c"].Value)
	}
	if m["gpu.power_w"].Value != 3.21 {
		t.Errorf("power = %v", m["gpu.power_w"].Value)
	}
}

func TestGPUGarKeinSpeicherWirdGemeldet(t *testing.T) {
	m, events := collect(t, fakeSMI(t, noMemorySMI))

	if _, ok := m["gpu.mem_total_bytes"]; ok {
		t.Error("ohne Daten darf kein Speicherwert erfunden werden")
	}
	if m["gpu.mem_unavailable"].Value != 1 {
		t.Error("fehlender Speicher muss ausdrücklich gemeldet werden - " +
			"sonst sieht es im Dashboard wie ein Agent-Ausfall aus")
	}
	found := false
	for _, k := range events {
		if k == "gpu.memory_unavailable" {
			found = true
		}
	}
	if !found {
		t.Errorf("erklärendes Ereignis fehlt, Ereignisse: %v", events)
	}
}

func TestGPUErklaerungNurEinmal(t *testing.T) {
	g := NewGPUCollector()
	g.Binary = fakeSMI(t, noMemorySMI)
	ctx := context.Background()

	b1, _ := g.Collect(ctx, 1)
	b2, _ := g.Collect(ctx, 2)
	if len(b1.Events) == 0 {
		t.Fatal("erster Durchlauf ohne Ereignis")
	}
	if len(b2.Events) != 0 {
		t.Error("dieselbe Erklärung darf nicht bei jedem Scrape erneut kommen")
	}
}

func TestGPUOhneTreiberStillAbschalten(t *testing.T) {
	g := NewGPUCollector()
	g.Binary = filepath.Join(t.TempDir(), "gibt-es-nicht")
	b, err := g.Collect(context.Background(), 1)
	if err != nil {
		t.Fatalf("fehlender Treiber ist kein Fehler: %v", err)
	}
	if len(b.Samples) != 0 || len(b.Events) != 0 {
		t.Errorf("ohne NVIDIA-Treiber darf nichts gemeldet werden: %+v", b)
	}
	// Beim zweiten Mal gar nicht mehr versuchen
	if !g.absent {
		t.Error("Collector hätte sich abschalten müssen")
	}
}
