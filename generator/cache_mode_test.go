package main

import (
	"io"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"
)

func TestCacheHitOrMissContract(t *testing.T) {
	if _, err := parseOptions([]string{"--mode", "throughput", "--cases", "x", "--workers", "1", "--expected-cache", "hit-or-miss"}); err != nil {
		t.Fatal(err)
	}
	for _, tc := range []struct {
		name    string
		headers []string
		body    string
		valid   bool
	}{
		{"hit", []string{"hit"}, `{"n":1}`, true},
		{"miss", []string{"miss"}, `{"n":1}`, true},
		{"bypass", []string{"bypass"}, `{"n":1}`, false},
		{"missing", nil, `{"n":1}`, false},
		{"duplicate", []string{"hit", "hit"}, `{"n":1}`, false},
		{"combined", []string{"hit, miss"}, `{"n":1}`, false},
		{"wrongbody", []string{"hit"}, `{"n":2}`, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				time.Sleep(time.Millisecond)
				w.Header().Set("Content-Type", "application/json")
				for _, h := range tc.headers {
					w.Header().Add("X-Cache", h)
				}
				io.WriteString(w, tc.body)
			}))
			defer server.Close()
			args := throughputArgs(t, server.URL, 1)
			args.expectedCache = "hit-or-miss"
			args.warmup = 2
			args.duration = .02
			got, err := run(args)
			if err != nil {
				t.Fatal(err)
			}
			for _, phase := range []string{"warmup", "in_window"} {
				counts := got[phase].(map[string]any)
				if counts["terminal_completions"].(int64) == 0 {
					t.Fatal("no completions")
				}
				errors := counts["errors"].(int64)
				if (errors == 0) != tc.valid {
					t.Fatal(phase, got)
				}
			}
		})
	}
}
