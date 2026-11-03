package collector

import (
	"bufio"
	"strconv"
	"strings"
)

// Zweiter Weg an die Speicherwerte: `nvidia-smi -q -d MEMORY`.
//
// Warum es den braucht: auf Systemen mit gemeinsamem Speicher (Grace
// Blackwell / DGX Spark) liefert `--query-gpu=memory.total` ein "N/A", der
// Abschnitt "FB Memory Usage" in der ausführlichen Ausgabe aber sehr wohl
// Zahlen. Ohne diesen Weg stünde im Dashboard nur ein Strich, und zwar
// ausgerechnet bei der Zahl, die bei Inferenz am meisten zählt.

type GPUMemory struct {
	Index    int
	BusID    string
	TotalMiB float64
	UsedMiB  float64
	FreeMiB  float64
	// ReservedMiB ist das, was der Treiber selbst belegt.
	ReservedMiB float64
	HasTotal    bool
	HasUsed     bool
}

// ParseNvidiaSmiMemory liest die Ausgabe von `nvidia-smi -q -d MEMORY`.
//
// Die GPUs kommen in Index-Reihenfolge; die Abschnittsüberschrift ist
// "GPU <BusID>" ohne Einrückung. Innerhalb zählt nur "FB Memory Usage" -
// "BAR1 Memory Usage" und "Conf Compute Protected Memory Usage" beschreiben
// anderes und würden die Zahlen verfälschen.
func ParseNvidiaSmiMemory(out string) []GPUMemory {
	var gpus []GPUMemory
	var cur *GPUMemory
	section := ""

	sc := bufio.NewScanner(strings.NewReader(out))
	sc.Buffer(make([]byte, 0, 64*1024), 4*1024*1024)
	for sc.Scan() {
		raw := sc.Text()
		line := strings.TrimSpace(raw)
		if line == "" {
			continue
		}
		indent := len(raw) - len(strings.TrimLeft(raw, " \t"))

		if indent == 0 && strings.HasPrefix(line, "GPU ") {
			if cur != nil {
				gpus = append(gpus, *cur)
			}
			cur = &GPUMemory{Index: len(gpus), BusID: strings.TrimSpace(line[4:])}
			section = ""
			continue
		}
		if cur == nil {
			continue
		}

		if !strings.Contains(line, ":") {
			// Abschnittsüberschrift, z. B. "FB Memory Usage"
			section = line
			continue
		}

		key, val, _ := strings.Cut(line, ":")
		key = strings.TrimSpace(key)
		val = strings.TrimSpace(val)

		if section != "FB Memory Usage" {
			continue
		}
		mib, ok := parseMiB(val)
		if !ok {
			continue
		}
		switch key {
		case "Total":
			cur.TotalMiB, cur.HasTotal = mib, true
		case "Used":
			cur.UsedMiB, cur.HasUsed = mib, true
		case "Free":
			cur.FreeMiB = mib
		case "Reserved":
			cur.ReservedMiB = mib
		}
	}
	if cur != nil {
		gpus = append(gpus, *cur)
	}
	return gpus
}

// parseMiB liest "23034 MiB", "22 GiB" oder "N/A".
func parseMiB(v string) (float64, bool) {
	if !valid(v) {
		return 0, false
	}
	fields := strings.Fields(v)
	if len(fields) == 0 {
		return 0, false
	}
	n, err := strconv.ParseFloat(fields[0], 64)
	if err != nil {
		return 0, false
	}
	unit := ""
	if len(fields) > 1 {
		unit = strings.ToLower(fields[1])
	}
	switch unit {
	case "gib", "gb":
		return n * 1024, true
	case "kib", "kb":
		return n / 1024, true
	case "b":
		return n / (1024 * 1024), true
	default: // MiB
		return n, true
	}
}
