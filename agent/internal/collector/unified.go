package collector

import (
	"bufio"
	"os"
	"runtime"
	"strconv"
	"strings"
)

// Erkennung von Systemen mit gemeinsamem Speicher.
//
// Auf Grace-Blackwell- und Tegra-Plattformen teilen sich CPU und GPU denselben
// physischen Speicher. nvidia-smi meldet dort für alle Speicherfelder "N/A" -
// nicht, weil etwas kaputt wäre, sondern weil es keinen eigenen VRAM-Pool
// gibt, über den es berichten könnte.
//
// Das ist wichtig zu unterscheiden: bei einer gewöhnlichen Karte wäre ein
// fehlender Speicherwert ein Defekt, hier ist er der Normalfall. Und die
// Zahl, auf die es ankommt, gibt es trotzdem - es ist der Arbeitsspeicher
// des Hosts. Wer dort ein Modell lädt, belegt genau den.

// Namensteile bekannter Plattformen mit gemeinsamem Speicher.
var unifiedGPUNames = []string{
	"gb10", "gb200", "gh200", // Grace Blackwell / Grace Hopper
	"orin", "xavier", "tegra", "thor", // Jetson
}

// DetectUnifiedMemory prüft, ob diese Maschine CPU und GPU denselben Speicher
// teilen lässt. `gpuNames` sind die Namen, die nvidia-smi gemeldet hat.
//
// Die Prüfung ist bewusst konservativ: im Zweifel `false`. Den Hauptspeicher
// als GPU-Speicher auszuweisen, wo es nicht stimmt, wäre schlimmer als eine
// Lücke - es würde eine Zahl erfinden, die plausibel aussieht.
func DetectUnifiedMemory(gpuNames []string) (bool, string) {
	for _, n := range gpuNames {
		l := strings.ToLower(n)
		for _, m := range unifiedGPUNames {
			if strings.Contains(l, m) {
				return true, "GPU-Modell " + n
			}
		}
	}
	// Jetson und verwandte Plattformen legen diese Datei an.
	if _, err := os.Stat("/etc/nv_tegra_release"); err == nil {
		return true, "/etc/nv_tegra_release vorhanden"
	}
	// Auf ARM-Systemen mit NVIDIA-SoC steht das Modell im Device-Tree.
	if runtime.GOARCH == "arm64" {
		if b, err := os.ReadFile("/proc/device-tree/model"); err == nil {
			model := strings.TrimRight(string(b), "\x00\n ")
			lm := strings.ToLower(model)
			if strings.Contains(lm, "nvidia") || strings.Contains(lm, "grace") ||
				strings.Contains(lm, "jetson") || strings.Contains(lm, "spark") {
				return true, "Device-Tree: " + model
			}
		}
	}
	return false, ""
}

// HostMemory liest Gesamt- und belegten Arbeitsspeicher aus /proc/meminfo.
// Auf Systemen mit gemeinsamem Speicher ist das die eigentliche Grenze für
// Modelle.
func HostMemory() (total, used float64, ok bool) {
	f, err := os.Open("/proc/meminfo")
	if err != nil {
		return 0, 0, false
	}
	defer f.Close()

	var memTotal, memAvailable float64
	var haveTotal, haveAvail bool
	sc := bufio.NewScanner(f)
	for sc.Scan() {
		key, rest, found := strings.Cut(sc.Text(), ":")
		if !found {
			continue
		}
		fields := strings.Fields(rest)
		if len(fields) == 0 {
			continue
		}
		v, err := strconv.ParseFloat(fields[0], 64)
		if err != nil {
			continue
		}
		if len(fields) > 1 && fields[1] == "kB" {
			v *= 1024
		}
		switch key {
		case "MemTotal":
			memTotal, haveTotal = v, true
		case "MemAvailable":
			memAvailable, haveAvail = v, true
		}
		if haveTotal && haveAvail {
			break
		}
	}
	if !haveTotal || !haveAvail {
		return 0, 0, false
	}
	return memTotal, memTotal - memAvailable, true
}
