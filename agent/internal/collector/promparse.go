package collector

import (
	"bufio"
	"io"
	"math"
	"strconv"
	"strings"
)

// PromSample ist eine Zeile im Prometheus-Textformat.
type PromSample struct {
	Name   string
	Labels map[string]string
	Value  float64
}

// ParsePromText liest das Prometheus-Textformat (Version 0.0.4).
//
// Eigener Parser statt der Prometheus-Bibliothek: der Agent soll eine statische
// Binary ohne Abhängigkeiten bleiben, und gebraucht wird nur die Lesefunktion.
// Nicht unterstützt: Exemplare, native Histogramme, Zeitstempel in der Zeile
// (werden ignoriert, der Wert wird trotzdem übernommen).
func ParsePromText(r io.Reader) ([]PromSample, error) {
	var out []PromSample
	sc := bufio.NewScanner(r)
	sc.Buffer(make([]byte, 0, 64*1024), 4*1024*1024)
	for sc.Scan() {
		line := strings.TrimSpace(sc.Text())
		if line == "" || line[0] == '#' {
			continue
		}
		s, ok := parsePromLine(line)
		if ok {
			out = append(out, s)
		}
	}
	return out, sc.Err()
}

func parsePromLine(line string) (PromSample, bool) {
	var s PromSample
	name := line
	labels := ""

	if i := strings.IndexByte(line, '{'); i >= 0 {
		j := strings.LastIndexByte(line, '}')
		if j < i {
			return s, false
		}
		name = line[:i]
		labels = line[i+1 : j]
		line = strings.TrimSpace(line[j+1:])
	} else {
		i := strings.IndexAny(line, " \t")
		if i < 0 {
			return s, false
		}
		name = line[:i]
		line = strings.TrimSpace(line[i:])
	}

	// Rest ist "<wert>" oder "<wert> <timestamp>"
	fields := strings.Fields(line)
	if len(fields) == 0 {
		return s, false
	}
	v, err := parseFloat(fields[0])
	if err != nil {
		return s, false
	}

	s.Name = strings.TrimSpace(name)
	s.Value = v
	if labels != "" {
		s.Labels = parsePromLabels(labels)
	}
	return s, s.Name != ""
}

func parseFloat(v string) (float64, error) {
	switch strings.ToLower(v) {
	case "+inf", "inf":
		return math.Inf(1), nil
	case "-inf":
		return math.Inf(-1), nil
	case "nan":
		// NaN würde der Server ablehnen; Zeile verwerfen.
		return 0, strconv.ErrSyntax
	}
	return strconv.ParseFloat(v, 64)
}

// parsePromLabels liest k="v",k2="v2" inklusive Escapes.
func parsePromLabels(s string) map[string]string {
	out := map[string]string{}
	i := 0
	for i < len(s) {
		for i < len(s) && (s[i] == ' ' || s[i] == ',') {
			i++
		}
		eq := strings.IndexByte(s[i:], '=')
		if eq < 0 {
			break
		}
		key := strings.TrimSpace(s[i : i+eq])
		i += eq + 1
		for i < len(s) && s[i] == ' ' {
			i++
		}
		if i >= len(s) || s[i] != '"' {
			break
		}
		i++
		var b strings.Builder
		for i < len(s) {
			c := s[i]
			if c == '\\' && i+1 < len(s) {
				i++
				switch s[i] {
				case 'n':
					b.WriteByte('\n')
				case 't':
					b.WriteByte('\t')
				case '\\':
					b.WriteByte('\\')
				case '"':
					b.WriteByte('"')
				default:
					b.WriteByte(s[i])
				}
				i++
				continue
			}
			if c == '"' {
				i++
				break
			}
			b.WriteByte(c)
			i++
		}
		if key != "" {
			out[key] = b.String()
		}
	}
	return out
}

// Index gruppiert Samples nach Metrikname für schnellen Zugriff.
type PromIndex map[string][]PromSample

func NewPromIndex(samples []PromSample) PromIndex {
	idx := PromIndex{}
	for _, s := range samples {
		idx[s.Name] = append(idx[s.Name], s)
	}
	return idx
}

// Sum addiert alle Serien einer Metrik (über Labeldimensionen hinweg).
func (p PromIndex) Sum(name string) (float64, bool) {
	ss, ok := p[name]
	if !ok || len(ss) == 0 {
		return 0, false
	}
	var t float64
	for _, s := range ss {
		t += s.Value
	}
	return t, true
}

// First gibt die erste Serie einer Metrik.
func (p PromIndex) First(name string) (PromSample, bool) {
	ss, ok := p[name]
	if !ok || len(ss) == 0 {
		return PromSample{}, false
	}
	return ss[0], true
}
