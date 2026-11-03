package collector

import "testing"

func TestErkenntGemeinsamenSpeicherAmGPUNamen(t *testing.T) {
	cases := []struct {
		names []string
		want  bool
	}{
		{[]string{"NVIDIA GB10"}, true},
		{[]string{"NVIDIA GH200 480GB"}, true},
		{[]string{"Orin (nvgpu)"}, true},
		{[]string{"NVIDIA L4"}, false},
		{[]string{"NVIDIA GeForce RTX 4090"}, false},
		{[]string{"NVIDIA RTX A6000"}, false},
		// Gemischt: eine einzige Unified-Karte genügt für die Erkennung
		{[]string{"NVIDIA L4", "NVIDIA GB10"}, true},
		{nil, false},
	}
	for _, c := range cases {
		got, reason := DetectUnifiedMemory(c.names)
		// Auf der Testmaschine darf die Dateisystem-Prüfung nicht anschlagen;
		// tut sie es doch, ist das Ergebnis für den Namensfall ohnehin true.
		if !c.want && got && reason != "" &&
			(reason[0] == '/' || reason[:5] == "Devic") {
			t.Skipf("Testmaschine ist selbst ein Unified-System (%s)", reason)
		}
		if got != c.want {
			t.Errorf("DetectUnifiedMemory(%v) = %v (%s), erwartet %v",
				c.names, got, reason, c.want)
		}
		if got && reason == "" {
			t.Errorf("Erkennung ohne Begründung für %v", c.names)
		}
	}
}

func TestHostMemory(t *testing.T) {
	total, used, ok := HostMemory()
	if !ok {
		t.Skip("/proc/meminfo nicht lesbar")
	}
	if total <= 0 {
		t.Errorf("MemTotal = %v", total)
	}
	if used < 0 || used > total {
		t.Errorf("belegt = %v, gesamt = %v – unplausibel", used, total)
	}
}
