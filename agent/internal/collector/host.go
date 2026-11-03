package collector

import (
	"bufio"
	"context"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"time"

	"github.com/aimonitor/agent/internal/model"
)

// Host-Metriken direkt aus /proc und /sys. Keine Fremdbibliothek, kein cgo.

type HostCollector struct {
	prevCPU    cpuTimes
	prevNet    map[string][2]uint64
	prevDisk   map[string][2]uint64
	prevTime   time.Time
	haveSample bool
	mounts     []string
}

type cpuTimes struct{ total, idle uint64 }

func NewHostCollector() *HostCollector {
	return &HostCollector{
		prevNet:  map[string][2]uint64{},
		prevDisk: map[string][2]uint64{},
	}
}

func (h *HostCollector) Name() string { return "host" }

func (h *HostCollector) Collect(ctx context.Context, now float64) (model.Batch, error) {
	var b model.Batch
	nowT := time.Now()
	elapsed := nowT.Sub(h.prevTime).Seconds()

	add := func(metric string, v float64, labels map[string]string) {
		b.Samples = append(b.Samples, model.Sample{
			Metric: metric, Value: v, TS: now, Labels: labels,
		})
	}

	// --- CPU
	if ct, ok := readCPU(); ok {
		if h.haveSample && ct.total > h.prevCPU.total {
			dt := float64(ct.total - h.prevCPU.total)
			di := float64(ct.idle - h.prevCPU.idle)
			add("host.cpu_pct", (1-di/dt)*100, nil)
		}
		h.prevCPU = ct
	}

	// --- Load
	if data, err := os.ReadFile("/proc/loadavg"); err == nil {
		f := strings.Fields(string(data))
		if len(f) >= 3 {
			for i, m := range []string{"host.load1", "host.load5", "host.load15"} {
				if v, err := strconv.ParseFloat(f[i], 64); err == nil {
					add(m, v, nil)
				}
			}
		}
		if len(f) >= 4 {
			if r, _, ok := strings.Cut(f[3], "/"); ok {
				if v, err := strconv.ParseFloat(r, 64); err == nil {
					add("host.procs_running", v, nil)
				}
			}
		}
	}

	// --- Speicher
	if mem := readMeminfo(); mem != nil {
		total := mem["MemTotal"]
		avail, hasAvail := mem["MemAvailable"]
		if total > 0 {
			add("host.mem_total_bytes", float64(total), nil)
			if hasAvail {
				add("host.mem_available_bytes", float64(avail), nil)
				add("host.mem_used_bytes", float64(total-avail), nil)
			}
		}
		if st, ok := mem["SwapTotal"]; ok && st > 0 {
			add("host.swap_total_bytes", float64(st), nil)
			if sf, ok := mem["SwapFree"]; ok {
				add("host.swap_used_bytes", float64(st-sf), nil)
			}
		}
	}

	// --- Uptime
	if data, err := os.ReadFile("/proc/uptime"); err == nil {
		if f := strings.Fields(string(data)); len(f) > 0 {
			if v, err := strconv.ParseFloat(f[0], 64); err == nil {
				add("host.uptime_s", v, nil)
			}
		}
	}

	// --- Dateisysteme
	for _, mp := range h.mountPoints() {
		var st syscall.Statfs_t
		if err := syscall.Statfs(mp, &st); err != nil {
			continue
		}
		bs := float64(st.Bsize)
		total := float64(st.Blocks) * bs
		if total <= 0 {
			continue
		}
		free := float64(st.Bavail) * bs
		lbl := map[string]string{"mount": mp}
		add("host.disk_total_bytes", total, lbl)
		add("host.disk_used_bytes", total-free, lbl)
		add("host.disk_free_bytes", free, lbl)
		if st.Files > 0 {
			add("host.inodes_used_pct",
				float64(st.Files-st.Ffree)/float64(st.Files)*100, lbl)
		}
	}

	// --- Netz
	if cur := readNet(); cur != nil {
		for iface, v := range cur {
			if prev, ok := h.prevNet[iface]; ok && elapsed > 0 {
				lbl := map[string]string{"iface": iface}
				if v[0] >= prev[0] {
					add("host.net_rx_bps", float64(v[0]-prev[0])/elapsed, lbl)
				}
				if v[1] >= prev[1] {
					add("host.net_tx_bps", float64(v[1]-prev[1])/elapsed, lbl)
				}
			}
		}
		h.prevNet = cur
	}

	// --- Blockgeräte
	if cur := readDiskstats(); cur != nil {
		for dev, v := range cur {
			if prev, ok := h.prevDisk[dev]; ok && elapsed > 0 {
				lbl := map[string]string{"dev": dev}
				if v[0] >= prev[0] {
					add("host.disk_read_bps", float64(v[0]-prev[0])*512/elapsed, lbl)
				}
				if v[1] >= prev[1] {
					add("host.disk_write_bps", float64(v[1]-prev[1])*512/elapsed, lbl)
				}
			}
		}
		h.prevDisk = cur
	}

	// --- Temperaturen
	for sensor, v := range readThermal() {
		add("host.cpu_temp_c", v, map[string]string{"sensor": sensor})
	}

	h.prevTime = nowT
	h.haveSample = true
	return b, nil
}

// mountPoints liefert echte Dateisysteme; tmpfs, overlay und Pseudo-FS raus.
func (h *HostCollector) mountPoints() []string {
	if h.mounts != nil {
		return h.mounts
	}
	skipFS := map[string]bool{
		"tmpfs": true, "devtmpfs": true, "proc": true, "sysfs": true,
		"cgroup": true, "cgroup2": true, "devpts": true, "mqueue": true,
		"debugfs": true, "tracefs": true, "securityfs": true, "pstore": true,
		"bpf": true, "configfs": true, "fusectl": true, "hugetlbfs": true,
		"squashfs": true, "ramfs": true, "autofs": true, "binfmt_misc": true,
		"nsfs": true, "overlay": true,
	}
	f, err := os.Open("/proc/mounts")
	if err != nil {
		h.mounts = []string{"/"}
		return h.mounts
	}
	defer f.Close()

	seen := map[string]bool{}
	var out []string
	sc := bufio.NewScanner(f)
	for sc.Scan() {
		fields := strings.Fields(sc.Text())
		if len(fields) < 3 || skipFS[fields[2]] {
			continue
		}
		mp := unescapeMount(fields[1])
		if seen[mp] || strings.HasPrefix(mp, "/proc") || strings.HasPrefix(mp, "/sys") ||
			strings.HasPrefix(mp, "/run") || strings.HasPrefix(mp, "/dev") ||
			strings.HasPrefix(mp, "/snap") || strings.HasPrefix(mp, "/var/lib/docker") {
			continue
		}
		seen[mp] = true
		out = append(out, mp)
		if len(out) >= 16 {
			break
		}
	}
	if len(out) == 0 {
		out = []string{"/"}
	}
	h.mounts = out
	return out
}

func unescapeMount(s string) string {
	r := strings.NewReplacer(`\040`, " ", `\011`, "\t", `\012`, "\n", `\134`, `\`)
	return r.Replace(s)
}

func readCPU() (cpuTimes, bool) {
	f, err := os.Open("/proc/stat")
	if err != nil {
		return cpuTimes{}, false
	}
	defer f.Close()
	sc := bufio.NewScanner(f)
	for sc.Scan() {
		line := sc.Text()
		if !strings.HasPrefix(line, "cpu ") {
			continue
		}
		fields := strings.Fields(line)[1:]
		var ct cpuTimes
		for i, fv := range fields {
			v, err := strconv.ParseUint(fv, 10, 64)
			if err != nil {
				continue
			}
			ct.total += v
			if i == 3 || i == 4 { // idle + iowait
				ct.idle += v
			}
		}
		return ct, ct.total > 0
	}
	return cpuTimes{}, false
}

func readMeminfo() map[string]uint64 {
	f, err := os.Open("/proc/meminfo")
	if err != nil {
		return nil
	}
	defer f.Close()
	out := map[string]uint64{}
	sc := bufio.NewScanner(f)
	for sc.Scan() {
		key, rest, ok := strings.Cut(sc.Text(), ":")
		if !ok {
			continue
		}
		fields := strings.Fields(rest)
		if len(fields) == 0 {
			continue
		}
		v, err := strconv.ParseUint(fields[0], 10, 64)
		if err != nil {
			continue
		}
		if len(fields) > 1 && fields[1] == "kB" {
			v *= 1024
		}
		out[key] = v
	}
	return out
}

func readNet() map[string][2]uint64 {
	f, err := os.Open("/proc/net/dev")
	if err != nil {
		return nil
	}
	defer f.Close()
	out := map[string][2]uint64{}
	sc := bufio.NewScanner(f)
	for sc.Scan() {
		name, rest, ok := strings.Cut(sc.Text(), ":")
		if !ok {
			continue
		}
		iface := strings.TrimSpace(name)
		if iface == "lo" || strings.HasPrefix(iface, "veth") ||
			strings.HasPrefix(iface, "br-") || strings.HasPrefix(iface, "docker") {
			continue
		}
		fields := strings.Fields(rest)
		if len(fields) < 9 {
			continue
		}
		rx, e1 := strconv.ParseUint(fields[0], 10, 64)
		tx, e2 := strconv.ParseUint(fields[8], 10, 64)
		if e1 == nil && e2 == nil {
			out[iface] = [2]uint64{rx, tx}
		}
	}
	return out
}

func readDiskstats() map[string][2]uint64 {
	f, err := os.Open("/proc/diskstats")
	if err != nil {
		return nil
	}
	defer f.Close()
	out := map[string][2]uint64{}
	sc := bufio.NewScanner(f)
	for sc.Scan() {
		fields := strings.Fields(sc.Text())
		if len(fields) < 10 {
			continue
		}
		dev := fields[2]
		// Partitionen und virtuelle Geräte überspringen.
		if strings.HasPrefix(dev, "loop") || strings.HasPrefix(dev, "ram") ||
			strings.HasPrefix(dev, "dm-") {
			continue
		}
		rd, e1 := strconv.ParseUint(fields[5], 10, 64)
		wr, e2 := strconv.ParseUint(fields[9], 10, 64)
		if e1 == nil && e2 == nil {
			out[dev] = [2]uint64{rd, wr}
		}
	}
	return out
}

func readThermal() map[string]float64 {
	out := map[string]float64{}
	zones, _ := filepath.Glob("/sys/class/thermal/thermal_zone*")
	for _, z := range zones {
		typeB, err := os.ReadFile(filepath.Join(z, "type"))
		if err != nil {
			continue
		}
		tempB, err := os.ReadFile(filepath.Join(z, "temp"))
		if err != nil {
			continue
		}
		v, err := strconv.ParseFloat(strings.TrimSpace(string(tempB)), 64)
		if err != nil {
			continue
		}
		name := strings.TrimSpace(string(typeB))
		if name == "" {
			name = filepath.Base(z)
		}
		out[name] = v / 1000.0
		if len(out) >= 8 {
			break
		}
	}
	return out
}
