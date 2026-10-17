package main

import (
	"net/http"
	"net/http/httptest"
	"reflect"
	"sync/atomic"
	"testing"
	"time"
)

func TestCheckedBinBoundaries(t *testing.T) {
	start := time.Now()
	end := start.Add(2500 * time.Millisecond)
	for _, c := range []struct {
		offset time.Duration
		want   int
	}{
		{-1, -1}, {0, 0}, {time.Second - 1, 0}, {time.Second, 1}, {2 * time.Second, 2}, {2500*time.Millisecond - 1, 2}, {2500 * time.Millisecond, -1},
	} {
		if got := checkedBinIndex(start, end, start.Add(c.offset)); got != c.want {
			t.Fatalf("offset=%v got=%d want=%d", c.offset, got, c.want)
		}
	}
}

func TestPerSecondSumsAndPartialBin(t *testing.T) {
	var requests atomic.Int64
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		n := requests.Add(1)
		time.Sleep(2 * time.Millisecond)
		if n%3 == 0 {
			w.WriteHeader(503)
			return
		}
		if n%3 == 1 {
			jsonResponse(w, `{"n":1}`)
		} else {
			jsonResponse(w, `{"n":2}`)
		}
	}))
	defer server.Close()
	args := throughputArgs(t, server.URL, 2)
	args.duration = 1.03
	args.warmup = 2
	result, err := run(args)
	if err != nil {
		t.Fatal(err)
	}
	bins := result["per_second"].([]map[string]any)
	if len(bins) != 2 || bins[0]["end_offset_seconds"] != float64(1) || bins[1]["start_offset_seconds"] != float64(1) || bins[1]["end_offset_seconds"] != args.duration {
		t.Fatal(bins)
	}
	sums := map[string]int64{}
	var check float64
	for _, bin := range bins {
		var total int64
		for category, count := range bin["terminal_counts"].(map[string]int64) {
			sums[category] += count
			total += count
		}
		if total != bin["terminal_completions"].(int64) || total != bin["successes"].(int64)+bin["errors"].(int64) {
			t.Fatal(bin)
		}
		width := bin["end_offset_seconds"].(float64) - bin["start_offset_seconds"].(float64)
		if bin["goodput_rps"].(float64) != float64(bin["successes"].(int64))/width {
			t.Fatal(bin)
		}
		check += bin["check_elapsed_seconds"].(float64)
	}
	if !reflect.DeepEqual(sums, result["in_window"].(map[string]any)["terminal_counts"]) {
		t.Fatal(sums, result)
	}
	if sums["success"] == 0 || sums["wrong_body"] == 0 || sums["http_errors"] == 0 || check <= 0 {
		t.Fatal(sums, check)
	}
	if result["warmup"].(map[string]any)["terminal_completions"].(int64) != 4 {
		t.Fatal(result)
	}
}

func TestCheckerTimingPreservesCategories(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/ok":
			jsonResponse(w, `{"n":1}`)
		case "/wrong":
			jsonResponse(w, `{"n":2}`)
		case "/invalid":
			jsonResponse(w, `{"n":`)
		case "/headers":
			w.Write([]byte(`{"n":1}`))
		case "/status":
			w.WriteHeader(503)
		}
	}))
	defer server.Close()
	args := throughputArgs(t, server.URL, 1)
	args.connections = 1
	client := clientFor(args)
	defer client.CloseIdleConnections()
	for _, c := range []struct {
		path, category string
		timed          bool
	}{
		{"/ok", "success", true}, {"/wrong", "wrong_body", true}, {"/invalid", "client_errors", true}, {"/headers", "bad_headers", true}, {"/status", "http_errors", false},
	} {
		got := execute(client, loadCase{path: c.path, address: server.URL + c.path, expected: checker(t, `{"n":1}`)}, args)
		if got.category != c.category || (got.checkElapsed > 0) != c.timed {
			t.Fatal(c, got)
		}
	}
}
