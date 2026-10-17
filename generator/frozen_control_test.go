package main

import (
	"encoding/json"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"sync/atomic"
	"testing"
	"time"
)

// HTTP_LOAD_BINARY overrides the container path for local runs against a separately built binary.
func frozenBinary() string {
	if path := os.Getenv("HTTP_LOAD_BINARY"); path != "" {
		return path
	}
	return "/frozen/http-load"
}

func TestIndependentFrozenBinary(t *testing.T) {
	for _, slow := range []bool{false, true} {
		t.Run(strconv.FormatBool(slow), func(t *testing.T) {
			var connections, requests, active, peak atomic.Int64
			srv := httptest.NewUnstartedServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				requests.Add(1)
				n := active.Add(1)
				defer active.Add(-1)
				for old := peak.Load(); n > old; old = peak.Load() {
					if peak.CompareAndSwap(old, n) {
						break
					}
				}
				if slow {
					time.Sleep(80 * time.Millisecond)
				} else {
					time.Sleep(2 * time.Millisecond)
				}
				w.Header().Set("Content-Type", "application/json")
				w.Header().Set("X-Cache", "bypass")
				w.Write([]byte(`{"n":1}`))
			}))
			srv.Config.ConnState = func(_ net.Conn, s http.ConnState) {
				if s == http.StateNew {
					connections.Add(1)
				}
			}
			srv.Start()
			defer srv.Close()
			cases := filepath.Join(t.TempDir(), "cases.json")
			if err := os.WriteFile(cases, []byte(`[{"path":"/","expected":{"n":1}}]`), 0600); err != nil {
				t.Fatal(err)
			}
			duration := "0.12"
			warmup := "2"
			if slow {
				duration = "0.02"
				warmup = "0"
			}
			cmd := exec.Command(frozenBinary(), "-mode", "throughput", "-workers", "4", "-duration", duration, "-warmup", warmup, "-url", srv.URL, "-cases", cases, "-expected-cache", "bypass", "-gomaxprocs", "2")
			raw, err := cmd.CombinedOutput()
			if err != nil {
				t.Fatalf("%v %s", err, raw)
			}
			var got map[string]any
			if err = json.Unmarshal(raw, &got); err != nil {
				t.Fatal(err)
			}
			in := got["in_window"].(map[string]any)
			drain := got["drain"].(map[string]any)
			warm := got["warmup"].(map[string]any)
			count := func(m map[string]any) float64 { return m["terminal_completions"].(float64) }
			if float64(requests.Load()) != count(in)+count(drain)+count(warm) {
				t.Fatal("server/client count mismatch", got)
			}
			if got["requests_started"].(float64) != count(in)+count(drain) {
				t.Fatal("terminal count mismatch")
			}
			if connections.Load() > 4 || peak.Load() > 4 {
				t.Fatal("connection/concurrency limit", connections.Load(), peak.Load())
			}
			if in["errors"].(float64) != 0 || drain["errors"].(float64) != 0 || warm["errors"].(float64) != 0 {
				t.Fatal("errors", got)
			}
			success := float64(0)
			if v := in["terminal_counts"].(map[string]any)["success"]; v != nil {
				success = v.(float64)
			}
			if got["goodput_rps"].(float64) != success/got["duration_seconds"].(float64) {
				t.Fatal("formula")
			}
			if slow {
				if count(in) != 0 || count(drain) != 4 || got["goodput_rps"].(float64) != 0 {
					t.Fatal("drain included", got)
				}
			} else {
				if count(warm) != 8 || requests.Load() <= connections.Load()*2 {
					t.Fatal("warmup/reuse", got)
				}
			}
			t.Logf("slow=%v server_requests=%d connections=%d max_active=%d warmup=%v in=%v drain=%v rps=%v", slow, requests.Load(), connections.Load(), peak.Load(), count(warm), count(in), count(drain), got["goodput_rps"])
		})
	}
}
