package main

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

func caseFile(t *testing.T, raw string) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "cases.json")
	if err := os.WriteFile(path, []byte(raw), 0600); err != nil {
		t.Fatal(err)
	}
	return path
}

func TestDuplicateJSONKeys(t *testing.T) {
	for _, raw := range []string{`{"n":1,"n":1}`, `{"n":2,"n":1}`, `{"n":1,"\u006e":1}`,
		`{"items":[{"x":1,"x":1}]}`, `{"😀":1,"\ud83d\ude00":1}`} {
		if _, err := decodeJSON([]byte(raw)); err == nil {
			t.Fatalf("accepted duplicate: %s", raw)
		}
	}
	if _, err := decodeJSON([]byte(`[{"n":1},{"n":2}]`)); err != nil {
		t.Fatal(err)
	}
	for _, raw := range []string{`[{"path":"/","path":"/other","expected":1}]`,
		`[{"path":"/","params":{"x":"1","x":"2"},"expected":1}]`,
		`[{"path":"/","expected":{"n":1,"n":1}}]`} {
		if _, err := loadCases(caseFile(t, raw), "http://localhost"); err == nil {
			t.Fatal("accepted duplicate cases metadata")
		}
	}
}

func TestThroughputCLI(t *testing.T) {
	base := []string{"--mode", "throughput", "--workers", "3", "--cases", "cases.json", "--gomaxprocs", "2", "--expected-cache", "bypass"}
	args, err := parseOptions(base)
	if err != nil || args.mode != "throughput" || args.workers != 3 || args.procs != 2 {
		t.Fatal(args, err)
	}
	for _, extra := range [][]string{{"--rps", "0"}, {"--connections", "3"}, {"--max-inflight", "3"}, {"--p99-ms", "200"},
		{"--error-budget", "0"}, {"--max-lag-ms", "50"}, {"--workers", "0"}, {"--workers", "4097"}, {"--duration", "NaN"},
		{"--duration", "3601"}, {"--deadline", "121"}, {"--max-body-bytes", "8388609"}, {"--expected-cache", "whatever"}, {"positional"}} {
		if _, err := parseOptions(append(append([]string{}, base...), extra...)); err == nil {
			t.Fatalf("accepted invalid flags %v", extra)
		}
	}
	if _, err := parseOptions([]string{"--cases", "x", "--rps", "800"}); err != nil {
		t.Fatal(err)
	}
	if _, err := parseOptions([]string{"--cases", "x", "--rps", "800", "--workers", "1"}); err == nil {
		t.Fatal("ignored workers in open-loop")
	}
	if _, err := parseOptions([]string{"--cases", "x", "--mode", "other"}); err == nil {
		t.Fatal("accepted unknown mode")
	}
}

func throughputArgs(t *testing.T, address string, workers int) options {
	return options{mode: "throughput", workers: workers, address: address, cases: caseFile(t, `[{"path":"/","expected":{"n":1}}]`),
		duration: .05, phase: .2, deadline: .3, maxBody: 1024, procs: 2, expectedCache: "bypass"}
}

func jsonResponse(w http.ResponseWriter, raw string) {
	w.Header().Set("Content-Type", "application/json; charset=UTF-8")
	w.Header().Set("X-Cache", "bypass")
	io.WriteString(w, raw)
}

func TestClosedLoopWorkerBoundAndWarmupBarrier(t *testing.T) {
	var active, maxActive, total, lastWarmCompletion atomic.Int64
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		current := active.Add(1)
		defer active.Add(-1)
		for prior := maxActive.Load(); current > prior; prior = maxActive.Load() {
			if maxActive.CompareAndSwap(prior, current) {
				break
			}
		}
		request := total.Add(1)
		time.Sleep(5 * time.Millisecond)
		jsonResponse(w, `{"n":1}`)
		if request <= 6 {
			completed := time.Now().UnixNano()
			for prior := lastWarmCompletion.Load(); completed > prior; prior = lastWarmCompletion.Load() {
				if lastWarmCompletion.CompareAndSwap(prior, completed) {
					break
				}
			}
		}
	}))
	defer server.Close()
	args := throughputArgs(t, server.URL, 3)
	args.warmup = 2
	result, err := run(args)
	if err != nil {
		t.Fatal(err)
	}
	in := result["in_window"].(map[string]any)
	drain := result["drain"].(map[string]any)
	warm := result["warmup"].(map[string]any)
	started := result["requests_started"].(int64)
	if result["measurement_start_unix"].(float64)*1e9+1000 < float64(lastWarmCompletion.Load()) {
		t.Fatal("measurement before warmup completion", result)
	}
	if warm["terminal_completions"].(int64) != 6 || warm["errors"].(int64) != 0 || started+6 != total.Load() {
		t.Fatal(result, total.Load())
	}
	if in["terminal_completions"].(int64)+drain["terminal_completions"].(int64) != started || in["errors"].(int64) != 0 || drain["errors"].(int64) != 0 {
		t.Fatal(result)
	}
	if maxActive.Load() != 3 || result["client"].(map[string]any)["connections"].(int) != 3 {
		t.Fatal("worker/connection budget", maxActive.Load(), result)
	}
	good := in["terminal_counts"].(map[string]int64)["success"]
	if good == 0 || result["goodput_rps"].(float64) != float64(good)/args.duration {
		t.Fatal(result)
	}
	for _, key := range []string{"offered_rps", "offered", "slo_pass", "thresholds", "generator_schedule_ok", "error_fraction_of_offered", "arrival_lag_ms"} {
		if _, ok := result[key]; ok {
			t.Fatalf("legacy field %s in throughput result", key)
		}
	}
	if _, ok := in["terminal_counts"].(map[string]int64)["drops"]; ok {
		t.Fatal("throughput drops")
	}
	if _, err := json.Marshal(result); err != nil {
		t.Fatal(err)
	}
}

func TestMeasurementEndDoesNotCancelInFlight(t *testing.T) {
	var cancelled, requests atomic.Int64
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		requests.Add(1)
		select {
		case <-r.Context().Done():
			cancelled.Add(1)
		case <-time.After(80 * time.Millisecond):
			jsonResponse(w, `{"n":1}`)
		}
	}))
	defer server.Close()
	args := throughputArgs(t, server.URL, 2)
	args.duration = .02
	result, err := run(args)
	if err != nil {
		t.Fatal(err)
	}
	in := result["in_window"].(map[string]any)
	drain := result["drain"].(map[string]any)
	if result["goodput_rps"].(float64) != 0 || in["terminal_completions"].(int64) != 0 || drain["terminal_counts"].(map[string]int64)["success"] != 2 {
		t.Fatal(result)
	}
	if requests.Load() != 2 || result["requests_started"].(int64) != 2 || cancelled.Load() != 0 || result["drain_seconds"].(float64) < .04 {
		t.Fatal("window/drain", result, requests.Load(), cancelled.Load())
	}
}

func TestWrongTargetsKeepWarmupAndMeasuredErrors(t *testing.T) {
	for _, c := range []struct {
		name, body, media, cache, category string
		status                             int
	}{
		{"status", `{"n":1}`, "application/json", "bypass", "http_errors", 503},
		{"body", `{"n":true}`, "application/json", "bypass", "wrong_body", 200},
		{"duplicate", `{"n":1,"n":1}`, "application/json", "bypass", "client_errors", 200},
		{"missing cache", `{"n":1}`, "application/json", "", "bad_headers", 200},
		{"wrong cache", `{"n":1}`, "application/json", "hit", "bad_headers", 200},
		{"media", `{"n":1}`, "text/plain", "bypass", "bad_headers", 200},
		{"charset", `{"n":1}`, "application/json; charset=utf-16", "bypass", "bad_headers", 200},
		{"empty charset", `{"n":1}`, `application/json; charset=""`, "bypass", "bad_headers", 200},
	} {
		t.Run(c.name, func(t *testing.T) {
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				time.Sleep(time.Millisecond)
				w.Header().Set("Content-Type", c.media)
				if c.cache != "" {
					w.Header().Set("X-Cache", c.cache)
				}
				w.WriteHeader(c.status)
				io.WriteString(w, c.body)
			}))
			defer server.Close()
			args := throughputArgs(t, server.URL, 1)
			args.warmup = 2
			args.duration = .015
			result, err := run(args)
			if err != nil {
				t.Fatal("bad target should produce measured error report", err)
			}
			warm := result["warmup"].(map[string]any)
			in := result["in_window"].(map[string]any)
			if warm["terminal_counts"].(map[string]int64)[c.category] != 2 || in["terminal_counts"].(map[string]int64)[c.category] == 0 || result["goodput_rps"].(float64) != 0 {
				t.Fatal(result)
			}
		})
	}
}

func TestClosedLoopNetworkAndDeadlineFailures(t *testing.T) {
	for _, kind := range []string{"disconnect", "deadline"} {
		t.Run(kind, func(t *testing.T) {
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if kind == "disconnect" {
					connection, _, _ := w.(http.Hijacker).Hijack()
					connection.Close()
					return
				}
				w.Header().Set("Content-Type", "application/json")
				w.WriteHeader(200)
				w.(http.Flusher).Flush()
				<-r.Context().Done()
			}))
			defer server.Close()
			args := throughputArgs(t, server.URL, 1)
			args.warmup = 1
			args.duration = .01
			args.phase = .04
			args.deadline = .02
			result, err := run(args)
			if err != nil {
				t.Fatal(err)
			}
			if result["goodput_rps"].(float64) != 0 || result["warmup"].(map[string]any)["errors"].(int64) != 1 {
				t.Fatal(result)
			}
			in := result["in_window"].(map[string]any)
			drain := result["drain"].(map[string]any)
			if in["errors"].(int64)+drain["errors"].(int64) != result["requests_started"].(int64) {
				t.Fatal(result)
			}
		})
	}
}

func TestStrictJSONMalformedTokens(t *testing.T) {
	for _, raw := range []string{`{"n":1,}`, `[1,]`, `{"n"}`, `{"n":}`, `[}`, `{"n":1]`, `1 2`, `truefalse`} {
		if _, err := decodeJSON([]byte(raw)); err == nil {
			t.Fatalf("accepted %s", raw)
		}
	}
	if _, err := decodeJSON([]byte(strings.Repeat("[", 10001) + "1" + strings.Repeat("]", 10001))); err == nil {
		t.Fatal("unbounded nesting")
	}
}

func TestAmbiguousSuccessHeaders(t *testing.T) {
	for _, name := range []string{"Content-Type", "X-Cache"} {
		t.Run(name, func(t *testing.T) {
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				w.Header().Set("Content-Type", "application/json")
				w.Header().Set("X-Cache", "bypass")
				w.Header().Add(name, "other")
				io.WriteString(w, `{"n":1}`)
			}))
			defer server.Close()
			args := throughputArgs(t, server.URL, 1)
			client := clientFor(options{connections: 1, phase: .2, deadline: .3})
			defer client.CloseIdleConnections()
			result := execute(client, loadCase{path: "/", address: server.URL, expected: checker(t, `{"n":1}`)}, args)
			if result.category != "bad_headers" {
				t.Fatal(result)
			}
		})
	}
}
