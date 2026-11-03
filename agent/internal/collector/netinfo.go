package collector

import (
	"net"
	"os"
	"sort"
	"strings"
)

// Adressen des Hosts ermitteln, damit man sich vom Dashboard aus per SSH
// aufschalten kann, ohne erst in der Inventarliste nachzusehen.
//
// Gemeldet werden nur Adressen, über die der Host tatsächlich erreichbar ist:
// Loopback, Link-Local und Docker-/Container-Brücken fallen raus. Letztere
// wären aus Sicht eines anderen Rechners nutzlos.

// LocalAddresses gibt die brauchbaren IP-Adressen zurück, beste zuerst.
func LocalAddresses() []string {
	ifaces, err := net.Interfaces()
	if err != nil {
		return nil
	}
	type cand struct {
		ip    string
		score int
	}
	var out []cand

	for _, ifc := range ifaces {
		if ifc.Flags&net.FlagUp == 0 || ifc.Flags&net.FlagLoopback != 0 {
			continue
		}
		// Container- und VM-Brücken übergehen: deren Adressen sind von außen
		// nicht erreichbar. Overlay-Netze wie tailscale oder wg bleiben drin,
		// über die kommt man sehr wohl auf den Host.
		name := ifc.Name
		skip := false
		for _, pre := range []string{"docker", "br-", "veth", "virbr",
			"cni", "flannel", "kube", "cali"} {
			if strings.HasPrefix(name, pre) {
				skip = true
				break
			}
		}
		if skip {
			continue
		}
		addrs, err := ifc.Addrs()
		if err != nil {
			continue
		}
		for _, a := range addrs {
			ipnet, ok := a.(*net.IPNet)
			if !ok {
				continue
			}
			ip := ipnet.IP
			if ip.IsLoopback() || ip.IsLinkLocalUnicast() || ip.IsLinkLocalMulticast() {
				continue
			}
			// Öffentliche vor privaten, IPv4 vor IPv6: in dieser Reihenfolge
			// ist die erste Adresse meist die, die man wirklich benutzt.
			score := 0
			if ip.To4() != nil {
				score += 10
			}
			if !ip.IsPrivate() {
				score += 5
			}
			out = append(out, cand{ip.String(), score})
		}
	}
	sort.SliceStable(out, func(i, j int) bool { return out[i].score > out[j].score })

	seen := map[string]bool{}
	var ips []string
	for _, c := range out {
		if seen[c.ip] {
			continue
		}
		seen[c.ip] = true
		ips = append(ips, c.ip)
		if len(ips) >= 6 {
			break
		}
	}
	return ips
}

// SSHPort liest den Port aus /etc/ssh/sshd_config. Steht dort nichts,
// ist es der Standardport.
func SSHPort() string {
	return sshPortFrom("/etc/ssh/sshd_config")
}

func sshPortFrom(path string) string {
	data, err := os.ReadFile(path)
	if err != nil {
		return "22"
	}
	for _, line := range strings.Split(string(data), "\n") {
		line = strings.TrimSpace(line)
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		fields := strings.Fields(line)
		if len(fields) >= 2 && strings.EqualFold(fields[0], "Port") {
			return fields[1]
		}
	}
	return "22"
}

// LoginUser ist der Benutzer, unter dem der Agent läuft - als Vorschlag für
// den SSH-Befehl. Läuft der Agent als root (der Normalfall, er braucht
// nvidia-smi), ist das selten der Benutzer, mit dem man sich anmeldet;
// dann wird SUDO_USER bevorzugt, falls vorhanden.
func LoginUser() string {
	if u := os.Getenv("SUDO_USER"); u != "" && u != "root" {
		return u
	}
	if u := os.Getenv("USER"); u != "" {
		return u
	}
	return ""
}
