package collector

import (
	"net"
	"os"
	"path/filepath"
	"testing"
)

func TestLocalAddressesLiefertBrauchbares(t *testing.T) {
	ips := LocalAddresses()
	// In einem Container kann es durchaus keine geben; dann ist nil richtig.
	for _, s := range ips {
		ip := net.ParseIP(s)
		if ip == nil {
			t.Errorf("keine gültige IP: %q", s)
			continue
		}
		if ip.IsLoopback() {
			t.Errorf("Loopback hat hier nichts verloren: %s", s)
		}
		if ip.IsLinkLocalUnicast() {
			t.Errorf("Link-Local ist für SSH nutzlos: %s", s)
		}
	}
	if len(ips) > 6 {
		t.Errorf("Liste nicht begrenzt: %d Adressen", len(ips))
	}
}

func TestLocalAddressesOhneDoppelte(t *testing.T) {
	seen := map[string]bool{}
	for _, s := range LocalAddresses() {
		if seen[s] {
			t.Errorf("doppelte Adresse: %s", s)
		}
		seen[s] = true
	}
}

func TestSSHPortStandard(t *testing.T) {
	// Ohne lesbare Konfiguration muss der Standardport herauskommen,
	// nicht ein leerer String.
	if p := SSHPort(); p == "" {
		t.Error("leerer Port")
	}
}

func TestSSHPortAusKonfiguration(t *testing.T) {
	dir := t.TempDir()
	cfg := filepath.Join(dir, "sshd_config")
	content := "# Kommentar\n#Port 99\n\nPort 2222\nPermitRootLogin no\n"
	if err := os.WriteFile(cfg, []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}
	if got := sshPortFrom(cfg); got != "2222" {
		t.Errorf("Port = %q, erwartet 2222 (auskommentiertes Port 99 ignorieren)", got)
	}
	if got := sshPortFrom(filepath.Join(dir, "fehlt")); got != "22" {
		t.Errorf("ohne Datei = %q, erwartet 22", got)
	}
}
