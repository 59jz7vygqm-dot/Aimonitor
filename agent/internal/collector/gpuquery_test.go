package collector

import "testing"

// Ausgabe einer gewöhnlichen Karte mit eigenem Speicher.
const smiMemoryNormal = `
==============NVSMI LOG==============

Timestamp                                 : Mon Nov  3 10:30:00 2026
Driver Version                            : 550.90.07
CUDA Version                              : 12.4

Attached GPUs                             : 2
GPU 00000000:01:00.0
    FB Memory Usage
        Total                             : 23034 MiB
        Reserved                          : 512 MiB
        Used                              : 21100 MiB
        Free                              : 1422 MiB
    BAR1 Memory Usage
        Total                             : 256 MiB
        Used                              : 12 MiB
        Free                              : 244 MiB

GPU 00000000:02:00.0
    FB Memory Usage
        Total                             : 23034 MiB
        Reserved                          : 512 MiB
        Used                              : 17920 MiB
        Free                              : 4602 MiB
    BAR1 Memory Usage
        Total                             : 256 MiB
        Used                              : 8 MiB
        Free                              : 248 MiB
`

// Gemeinsamer Speicher: --query-gpu liefert N/A, der Abschnitt hier Zahlen.
const smiMemoryUnified = `
==============NVSMI LOG==============

Attached GPUs                             : 1
GPU 00000009:01:00.0
    FB Memory Usage
        Total                             : 119808 MiB
        Reserved                          : 2048 MiB
        Used                              : 3200 MiB
        Free                              : 114560 MiB
    BAR1 Memory Usage
        Total                             : N/A
        Used                              : N/A
        Free                              : N/A
    Conf Compute Protected Memory Usage
        Total                             : N/A
`

const smiMemoryAllNA = `
Attached GPUs                             : 1
GPU 00000009:01:00.0
    FB Memory Usage
        Total                             : N/A
        Used                              : N/A
        Free                              : N/A
`

func TestParseMemoryNormal(t *testing.T) {
	g := ParseNvidiaSmiMemory(smiMemoryNormal)
	if len(g) != 2 {
		t.Fatalf("erwartet 2 GPUs, bekommen %d", len(g))
	}
	if g[0].Index != 0 || g[1].Index != 1 {
		t.Errorf("Indizes falsch: %d %d", g[0].Index, g[1].Index)
	}
	if g[0].BusID != "00000000:01:00.0" {
		t.Errorf("BusID = %q", g[0].BusID)
	}
	if !g[0].HasTotal || g[0].TotalMiB != 23034 {
		t.Errorf("Total = %v (has=%v)", g[0].TotalMiB, g[0].HasTotal)
	}
	if !g[0].HasUsed || g[0].UsedMiB != 21100 {
		t.Errorf("Used = %v", g[0].UsedMiB)
	}
	if g[0].ReservedMiB != 512 {
		t.Errorf("Reserved = %v", g[0].ReservedMiB)
	}
	// BAR1 darf die FB-Werte nicht überschreiben - das ist ein anderer Speicher.
	if g[1].TotalMiB != 23034 || g[1].UsedMiB != 17920 {
		t.Errorf("GPU 1 von BAR1 verfälscht: %+v", g[1])
	}
}

func TestParseMemoryUnified(t *testing.T) {
	g := ParseNvidiaSmiMemory(smiMemoryUnified)
	if len(g) != 1 {
		t.Fatalf("erwartet 1 GPU, bekommen %d", len(g))
	}
	if !g[0].HasTotal || g[0].TotalMiB != 119808 {
		t.Errorf("Total = %v", g[0].TotalMiB)
	}
	if !g[0].HasUsed || g[0].UsedMiB != 3200 {
		t.Errorf("Used = %v", g[0].UsedMiB)
	}
}

func TestParseMemoryAllNA(t *testing.T) {
	g := ParseNvidiaSmiMemory(smiMemoryAllNA)
	if len(g) != 1 {
		t.Fatalf("erwartet 1 GPU, bekommen %d", len(g))
	}
	// Kein Wert vorhanden: das muss sichtbar sein, nicht als 0 durchgehen.
	if g[0].HasTotal || g[0].HasUsed {
		t.Errorf("N/A hätte nicht als Wert gelten dürfen: %+v", g[0])
	}
}

func TestParseMemoryLeer(t *testing.T) {
	if g := ParseNvidiaSmiMemory(""); len(g) != 0 {
		t.Errorf("leere Eingabe -> %d GPUs", len(g))
	}
}

func TestParseMiBEinheiten(t *testing.T) {
	cases := []struct {
		in   string
		want float64
		ok   bool
	}{
		{"23034 MiB", 23034, true},
		{"22 GiB", 22528, true},
		{"1024 KiB", 1, true},
		{"N/A", 0, false},
		{"[Not Supported]", 0, false},
		{"", 0, false},
		{"kaputt", 0, false},
	}
	for _, c := range cases {
		got, ok := parseMiB(c.in)
		if ok != c.ok || (ok && got != c.want) {
			t.Errorf("parseMiB(%q) = %v,%v – erwartet %v,%v", c.in, got, ok, c.want, c.ok)
		}
	}
}
