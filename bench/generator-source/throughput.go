package main

import (
	"net/http"
	"runtime"
	"sync"
	"time"
)

// Each worker owns its counters; aggregation occurs after the warmup/measurement barriers.
type throughputCounts struct {
	counts    map[string]int64
	cache     map[string]int64
	bytes     int64
	latencies []float64
	examples  []map[string]any
}

func (counts *throughputCounts) add(result outcome, item loadCase, started time.Time) {
	if counts.counts == nil {
		counts.counts = make(map[string]int64)
		counts.cache = make(map[string]int64)
	}
	counts.counts[result.category]++
	if result.category == "success" {
		counts.bytes += int64(result.bytes)
		counts.cache[result.cache]++
		if !started.IsZero() {
			counts.latencies = append(counts.latencies, result.received.Sub(started).Seconds()*1000)
		}
	} else if len(counts.examples) < 3 {
		counts.examples = append(counts.examples, map[string]any{"path": item.path, "category": result.category,
			"status": result.status, "error": result.detail, "content_type": result.media, "cache": result.cache})
	}
}

func (counts *throughputCounts) merge(other throughputCounts) {
	if counts.counts == nil {
		counts.counts = make(map[string]int64)
		counts.cache = make(map[string]int64)
	}
	for key, value := range other.counts {
		counts.counts[key] += value
	}
	for key, value := range other.cache {
		counts.cache[key] += value
	}
	counts.bytes += other.bytes
	counts.latencies = append(counts.latencies, other.latencies...)
	for _, example := range other.examples {
		if len(counts.examples) == 3 {
			break
		}
		counts.examples = append(counts.examples, example)
	}
}

func (counts throughputCounts) report(latency bool) map[string]any {
	var completions, failures int64
	for category, count := range counts.counts {
		completions += count
		if category != "success" {
			failures += count
		}
	}
	result := map[string]any{"terminal_counts": counts.counts, "terminal_completions": completions,
		"errors": failures, "successful_response_bytes": counts.bytes, "cache": counts.cache, "examples": counts.examples}
	if latency {
		result["successful_latency_ms"] = map[string]any{"p50": percentile(counts.latencies, .5),
			"p95": percentile(counts.latencies, .95), "p99": percentile(counts.latencies, .99)}
	}
	return result
}

func runThroughput(args options, cases []loadCase, client *http.Client) map[string]any {
	type workerResult struct {
		warmup, inWindow, drain throughputCounts
		started                 int64
	}
	results := make([]workerResult, args.workers)
	var ready, finished sync.WaitGroup
	ready.Add(args.workers)
	finished.Add(args.workers)
	barrier := make(chan struct{})
	var start, end time.Time
	for worker := range results {
		go func(worker int) {
			defer finished.Done()
			counts := &results[worker]
			for round := 0; round < args.warmup; round++ {
				for _, item := range cases {
					counts.warmup.add(execute(client, item, args), item, time.Time{})
				}
			}
			ready.Done()
			<-barrier
			for index := worker; ; index += args.workers {
				started := time.Now()
				if !started.Before(end) {
					break
				}
				counts.started++
				item := cases[index%len(cases)]
				result := execute(client, item, args)
				checked := time.Now() // Includes the complete body checker before classification.
				if checked.Before(end) {
					counts.inWindow.add(result, item, started)
				} else {
					counts.drain.add(result, item, started)
				}
			}
		}(worker)
	}
	ready.Wait() // All warmup requests, including errors, finish before measurement.
	start = time.Now()
	end = start.Add(time.Duration(args.duration * float64(time.Second)))
	close(barrier)
	if delay := time.Until(end); delay > 0 {
		time.Sleep(delay)
	}
	observedEnd := time.Now()
	finished.Wait() // No end-of-window cancellation; each request retains its total HTTP deadline.
	drained := time.Now()
	var warmup, inWindow, drain throughputCounts
	var started int64
	for _, result := range results {
		warmup.merge(result.warmup)
		inWindow.merge(result.inWindow)
		drain.merge(result.drain)
		started += result.started
	}
	return map[string]any{
		"kind": "full-http-closed-loop-throughput", "mode": "throughput",
		"generator":  "Go standard-library HTTP/1.1, every 200 body fully decoded and checked",
		"go_version": runtime.Version(), "goos": runtime.GOOS, "goarch": runtime.GOARCH,
		"gomaxprocs": runtime.GOMAXPROCS(0), "logical_cpus": runtime.NumCPU(),
		"workers": args.workers, "duration_seconds": args.duration, "requests_started": started,
		"measurement_start_unix": unix(start), "measurement_end_unix": unix(end),
		"observed_measurement_end_unix": unix(observedEnd), "drain_end_unix": unix(drained),
		"measurement_elapsed_seconds": args.duration, "drain_seconds": drained.Sub(end).Seconds(),
		"goodput_rps":      float64(inWindow.counts["success"]) / args.duration,
		"completion_scope": "after execute/full body checker within [measurement_start, measurement_end); drain excluded",
		"in_window":        inWindow.report(true), "drain": drain.report(true), "warmup": warmup.report(false),
		"warmup_rounds_per_worker": args.warmup,
		"latency_scope":            "diagnostic request start to full-body receipt; checked completion determines window; no latency/error threshold",
		"client": map[string]any{"connections": args.workers, "one_outstanding_per_worker": true,
			"phase_timeout_seconds": args.phase, "total_http_deadline_seconds": args.deadline,
			"max_response_body_bytes": args.maxBody, "expected_cache": args.expectedCache,
			"required_media_type": "application/json (absent charset or UTF-8)",
			"timeout_scope":       "connect/TLS/header phase plus total HTTP deadline; synchronous JSON/check CPU is not preempted by HTTP context",
			"redirects":           false, "environment_proxy": false, "compression": false, "protocol": "HTTP/1.1"},
		"body_check": "UTF-8, valid Unicode escapes, duplicate decoded keys rejected, UseNumber/EOF, exact big.Int integers and float64 coordinate semantics; no byte shortcut",
	}
}
