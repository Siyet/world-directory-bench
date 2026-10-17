// Private full-HTTP generator. Every successful response is decoded and checked.
package main

import (
	"bytes"
	"context"
	"crypto/tls"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"math"
	"math/big"
	"mime"
	"net"
	"net/http"
	"net/url"
	"os"
	"runtime"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"
	"unicode/utf8"
)

// encoding/json replaces malformed UTF-16 escapes; reject them before decoding.
// Escaped backslashes are consumed as escapes, not mistaken for a following \u.
func validUnicodeEscapes(raw []byte) bool {
	quoted := false
	for i := 0; i < len(raw); i++ {
		if !quoted {
			if raw[i] == '"' {
				quoted = true
			}
			continue
		}
		if raw[i] == '"' {
			quoted = false
			continue
		}
		if raw[i] != '\\' {
			continue
		}
		i++
		if i >= len(raw) {
			return false
		}
		if raw[i] != 'u' {
			continue
		}
		if i+4 >= len(raw) {
			return false
		}
		value, ok := hex16(raw[i+1 : i+5])
		if !ok {
			return false
		}
		i += 4
		if value >= 0xd800 && value <= 0xdbff {
			if i+6 >= len(raw) || raw[i+1] != '\\' || raw[i+2] != 'u' {
				return false
			}
			low, ok := hex16(raw[i+3 : i+7])
			if !ok || low < 0xdc00 || low > 0xdfff {
				return false
			}
			i += 6
		} else if value >= 0xdc00 && value <= 0xdfff {
			return false
		}
	}
	return true
}

func hex16(raw []byte) (uint16, bool) {
	var result uint16
	for _, char := range raw {
		var digit byte
		switch {
		case char >= '0' && char <= '9':
			digit = char - '0'
		case char >= 'a' && char <= 'f':
			digit = char - 'a' + 10
		case char >= 'A' && char <= 'F':
			digit = char - 'A' + 10
		default:
			return 0, false
		}
		result = result*16 + uint16(digit)
	}
	return result, true
}

func decodeJSON(raw []byte) (any, error) {
	if !utf8.Valid(raw) || !validUnicodeEscapes(raw) {
		return nil, errors.New("invalid Unicode JSON")
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.UseNumber()
	value, err := jsonValue(decoder, 0)
	if err != nil {
		return nil, err
	}
	if _, err := decoder.Token(); err != io.EOF {
		return nil, errors.New("trailing JSON or invalid suffix")
	}
	return value, nil
}

// Token decoding preserves Number types and checks keys before map insertion.
func jsonValue(decoder *json.Decoder, depth int) (any, error) {
	token, err := decoder.Token()
	if err != nil {
		return nil, err
	}
	delimiter, composite := token.(json.Delim)
	if !composite {
		return token, nil
	}
	if depth >= 10000 {
		return nil, errors.New("JSON nesting exceeds 10000")
	}
	switch delimiter {
	case '{':
		object := make(map[string]any)
		for decoder.More() {
			keyToken, err := decoder.Token()
			if err != nil {
				return nil, err
			}
			key, ok := keyToken.(string)
			if !ok {
				return nil, errors.New("JSON object key must be a string")
			}
			if _, exists := object[key]; exists {
				return nil, errors.New("duplicate JSON object key")
			}
			value, err := jsonValue(decoder, depth+1)
			if err != nil {
				return nil, err
			}
			object[key] = value
		}
		if token, err := decoder.Token(); err != nil || token != json.Delim('}') {
			return nil, errors.New("invalid JSON object ending")
		}
		return object, nil
	case '[':
		array := make([]any, 0)
		for decoder.More() {
			value, err := jsonValue(decoder, depth+1)
			if err != nil {
				return nil, err
			}
			array = append(array, value)
		}
		if token, err := decoder.Token(); err != nil || token != json.Delim(']') {
			return nil, errors.New("invalid JSON array ending")
		}
		return array, nil
	}
	return nil, errors.New("unexpected JSON delimiter")
}

func integerToken(number json.Number) bool { return !strings.ContainsAny(string(number), ".eE") }

type expectedValue struct {
	kind         string
	text         string
	boolean      bool
	integer      *big.Int
	floating     float64
	floatInteger *big.Int
	fields       map[string]*expectedValue
	items        []*expectedValue
}

func compileExpected(value any) (*expectedValue, error) {
	result := &expectedValue{}
	switch v := value.(type) {
	case nil:
		result.kind = "null"
	case bool:
		result.kind = "bool"
		result.boolean = v
	case string:
		result.kind = "string"
		result.text = v
	case json.Number:
		if integerToken(v) {
			result.kind = "integer"
			var ok bool
			result.integer, ok = new(big.Int).SetString(string(v), 10)
			if !ok {
				return nil, errors.New("invalid expected integer")
			}
		} else {
			result.kind = "float"
			parsed, err := strconv.ParseFloat(string(v), 64)
			if err != nil || math.IsNaN(parsed) || math.IsInf(parsed, 0) {
				return nil, errors.New("nonfinite expected float")
			}
			result.floating = parsed
			if math.Trunc(parsed) == parsed {
				result.floatInteger, _ = new(big.Float).SetFloat64(parsed).Int(nil)
			}
		}
	case map[string]any:
		result.kind = "object"
		result.fields = make(map[string]*expectedValue, len(v))
		for key, child := range v {
			checked, err := compileExpected(child)
			if err != nil {
				return nil, err
			}
			result.fields[key] = checked
		}
	case []any:
		result.kind = "array"
		result.items = make([]*expectedValue, len(v))
		for index, child := range v {
			checked, err := compileExpected(child)
			if err != nil {
				return nil, err
			}
			result.items[index] = checked
		}
	default:
		return nil, errors.New("unsupported expected JSON value")
	}
	return result, nil
}

func (expected *expectedValue) matches(actual any) bool {
	switch expected.kind {
	case "null":
		return actual == nil
	case "bool":
		value, ok := actual.(bool)
		return ok && value == expected.boolean
	case "string":
		value, ok := actual.(string)
		return ok && value == expected.text
	case "integer":
		value, ok := actual.(json.Number)
		if !ok || !integerToken(value) {
			return false
		}
		integer, ok := new(big.Int).SetString(string(value), 10)
		return ok && integer.Cmp(expected.integer) == 0
	case "float":
		value, ok := actual.(json.Number)
		if !ok {
			return false
		}
		if integerToken(value) {
			if expected.floatInteger == nil {
				return false
			}
			integer, ok := new(big.Int).SetString(string(value), 10)
			return ok && integer.Cmp(expected.floatInteger) == 0
		}
		floating, err := strconv.ParseFloat(string(value), 64)
		return (err == nil || errors.Is(err, strconv.ErrRange)) && !math.IsNaN(floating) && !math.IsInf(floating, 0) && floating == expected.floating
	case "object":
		value, ok := actual.(map[string]any)
		if !ok || len(value) != len(expected.fields) {
			return false
		}
		for key, child := range expected.fields {
			item, present := value[key]
			if !present || !child.matches(item) {
				return false
			}
		}
		return true
	case "array":
		value, ok := actual.([]any)
		if !ok || len(value) != len(expected.items) {
			return false
		}
		for index, child := range expected.items {
			if !child.matches(value[index]) {
				return false
			}
		}
		return true
	}
	return false
}

type fileCase struct {
	Path     string            `json:"path"`
	Params   map[string]string `json:"params"`
	Expected json.RawMessage   `json:"expected"`
}
type loadCase struct {
	path     string
	address  string
	expected *expectedValue
}

func loadCases(path, baseAddress string) ([]loadCase, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	if _, err := decodeJSON(raw); err != nil {
		return nil, fmt.Errorf("invalid cases JSON: %w", err)
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	var files []fileCase
	if err := decoder.Decode(&files); err != nil {
		return nil, err
	}
	var trailing any
	if err := decoder.Decode(&trailing); err != io.EOF {
		return nil, errors.New("trailing cases JSON")
	}
	if len(files) == 0 {
		return nil, errors.New("cases must be nonempty")
	}
	base, err := url.Parse(baseAddress)
	if err != nil || base.Host == "" || (base.Scheme != "http" && base.Scheme != "https") || base.User != nil {
		return nil, errors.New("need an HTTP(S) URL without userinfo")
	}
	result := make([]loadCase, len(files))
	for index, file := range files {
		reference, err := url.Parse(file.Path)
		if err != nil || file.Path == "" || reference.IsAbs() || reference.Host != "" {
			return nil, errors.New("cases need relative paths on the selected target")
		}
		address := base.ResolveReference(reference)
		query := address.Query()
		for key, value := range file.Params {
			query.Set(key, value)
		}
		address.RawQuery = query.Encode()
		decoded, err := decodeJSON(file.Expected)
		if err != nil {
			return nil, fmt.Errorf("case %s: %w", file.Path, err)
		}
		expected, err := compileExpected(decoded)
		if err != nil {
			return nil, err
		}
		result[index] = loadCase{file.Path, address.String(), expected}
	}
	return result, nil
}

type options struct {
	mode          string
	workers       int
	expectedCache string
	address       string
	cases         string
	rate          float64
	duration      float64
	phase         float64
	deadline      float64
	connections   int
	inflight      int
	warmup        int
	maxBody       int64
	procs         int
	p99           float64
	errorBudget   float64
	maxLag        float64
}

func clientFor(args options) *http.Client {
	dialer := &net.Dialer{Timeout: time.Duration(args.phase * float64(time.Second)), KeepAlive: 30 * time.Second}
	transport := &http.Transport{
		Proxy: nil, DialContext: dialer.DialContext,
		MaxConnsPerHost: args.connections, MaxIdleConns: args.connections, MaxIdleConnsPerHost: args.connections,
		IdleConnTimeout: 30 * time.Second, ResponseHeaderTimeout: time.Duration(args.phase * float64(time.Second)),
		TLSHandshakeTimeout: time.Duration(args.phase * float64(time.Second)),
		TLSNextProto:        make(map[string]func(string, *tls.Conn) http.RoundTripper),
		DisableCompression:  true,
	}
	return &http.Client{Transport: transport, CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}
}

type outcome struct {
	category string
	received time.Time
	bytes    int
	cache    string
	status   int
	media    string
	detail   string
}

func execute(client *http.Client, item loadCase, args options) outcome {
	ctx, cancel := context.WithTimeout(context.Background(), time.Duration(args.deadline*float64(time.Second)))
	defer cancel()
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, item.address, nil)
	if err != nil {
		return outcome{category: "client_errors", detail: err.Error()}
	}
	response, err := client.Do(request)
	if err != nil {
		return requestError(ctx, err)
	}
	raw, err := io.ReadAll(io.LimitReader(response.Body, args.maxBody+1))
	response.Body.Close()
	received := time.Now() // Full body receipt, before the mandatory checker, matching the Python latency scope.
	if err != nil {
		return requestError(ctx, err)
	}
	if len(raw) > int(args.maxBody) {
		return outcome{category: "client_errors", detail: "response body limit exceeded"}
	}
	if deadline, ok := ctx.Deadline(); ok && !received.Before(deadline) {
		return outcome{category: "deadline_timeouts", detail: "total HTTP deadline"}
	}
	cache := response.Header.Get("X-Cache")
	if cache == "" {
		cache = "missing"
	}
	media := response.Header.Get("Content-Type")
	if response.StatusCode != 200 {
		return outcome{category: "http_errors", status: response.StatusCode, cache: cache, media: media}
	}
	decoded, err := decodeJSON(raw)
	if err != nil {
		return outcome{category: "client_errors", status: 200, cache: cache, media: media, detail: "invalid response JSON: " + err.Error()}
	}
	if !item.expected.matches(decoded) {
		return outcome{category: "wrong_body", status: 200, cache: cache, media: media}
	}
	if args.mode == "throughput" {
		mediaType, parameters, err := mime.ParseMediaType(media)
		charset, hasCharset := parameters["charset"]
		if err != nil || len(response.Header.Values("Content-Type")) != 1 || mediaType != "application/json" || (hasCharset && !strings.EqualFold(charset, "utf-8")) {
			return outcome{category: "bad_headers", status: 200, media: media, cache: cache, detail: "expected UTF-8 application/json Content-Type"}
		}
		if args.expectedCache != "" && (cache != args.expectedCache || len(response.Header.Values("X-Cache")) != 1) {
			return outcome{category: "bad_headers", status: 200, media: media, cache: cache, detail: "unexpected X-Cache header"}
		}
	}
	return outcome{category: "success", received: received, bytes: len(raw), cache: cache, status: 200, media: media}
}

func requestError(ctx context.Context, err error) outcome {
	category := "client_errors"
	if errors.Is(ctx.Err(), context.DeadlineExceeded) {
		category = "deadline_timeouts"
	} else {
		var timeout net.Error
		if errors.As(err, &timeout) && timeout.Timeout() {
			category = "timeouts"
		}
	}
	return outcome{category: category, detail: err.Error()}
}

func percentile(values []float64, fraction float64) any {
	if len(values) == 0 {
		return nil
	}
	copyValues := append([]float64(nil), values...)
	sort.Float64s(copyValues)
	index := int(math.Ceil(float64(len(copyValues))*fraction)) - 1
	if index < 0 {
		index = 0
	}
	return copyValues[index]
}

func unix(timestamp time.Time) float64 {
	return float64(timestamp.Unix()) + float64(timestamp.Nanosecond())/1e9
}

func run(args options) (map[string]any, error) {
	cases, err := loadCases(args.cases, args.address)
	if err != nil {
		return nil, err
	}
	if args.procs > 0 {
		runtime.GOMAXPROCS(args.procs)
	}
	if args.mode == "throughput" {
		args.connections = args.workers
	}
	client := clientFor(args)
	defer client.CloseIdleConnections()
	if args.mode == "throughput" {
		return runThroughput(args, cases, client), nil
	}
	for round := 0; round < args.warmup; round++ {
		for _, item := range cases {
			result := execute(client, item, args)
			if result.category != "success" {
				return nil, fmt.Errorf("warmup contract failed %s: %s %s", item.path, result.category, result.detail)
			}
		}
	}
	counts := make(map[string]int64)
	cache := make(map[string]int64)
	latencies := make([]float64, 0)
	lags := make([]float64, 0)
	examples := make([]map[string]any, 0)
	var mutex sync.Mutex
	var workers sync.WaitGroup
	slots := make(chan struct{}, args.inflight)
	var successfulBytes int64
	var inWindow int64
	start := time.Now()
	end := start.Add(time.Duration(args.duration * float64(time.Second)))
	offered := int64(math.Ceil(args.rate * args.duration))
	for index := int64(0); index < offered; index++ {
		scheduled := start.Add(time.Duration(float64(index) / args.rate * float64(time.Second)))
		if delay := time.Until(scheduled); delay > 0 {
			time.Sleep(delay)
		}
		lags = append(lags, math.Max(0, float64(time.Since(scheduled))/float64(time.Millisecond)))
		select {
		case slots <- struct{}{}:
			mutex.Lock()
			counts["attempted"]++
			mutex.Unlock()
			item := cases[index%int64(len(cases))]
			workers.Add(1)
			go func(item loadCase, scheduled time.Time) {
				defer workers.Done()
				defer func() { <-slots }()
				result := execute(client, item, args)
				mutex.Lock()
				defer mutex.Unlock()
				counts[result.category]++
				if result.category == "success" {
					if result.received.Before(end) {
						inWindow++
					}
					successfulBytes += int64(result.bytes)
					cache[result.cache]++
					latencies = append(latencies, float64(result.received.Sub(scheduled))/float64(time.Millisecond))
				} else if len(examples) < 3 {
					examples = append(examples, map[string]any{"path": item.path, "category": result.category, "status": result.status, "error": result.detail})
				}
			}(item, scheduled)
		default:
			mutex.Lock()
			counts["drops"]++
			mutex.Unlock()
		}
	}
	if delay := time.Until(end); delay > 0 {
		time.Sleep(delay)
	}
	observedEnd := time.Now()
	workers.Wait()
	drained := time.Now()
	failures := int64(0)
	for _, key := range []string{"http_errors", "wrong_body", "timeouts", "deadline_timeouts", "pool_timeouts", "client_errors", "drops"} {
		failures += counts[key]
	}
	maxLag := 0.0
	for _, value := range lags {
		if value > maxLag {
			maxLag = value
		}
	}
	p99 := percentile(latencies, .99)
	generatorOK := maxLag <= args.maxLag
	errorFraction := float64(failures) / float64(offered)
	pass := generatorOK && errorFraction <= args.errorBudget && p99 != nil && p99.(float64) <= args.p99
	return map[string]any{
		"kind": "full-http-read-pilot", "claim_capacity": false,
		"generator":  "Go standard-library HTTP/1.1, all 200 bodies decoded and checked",
		"go_version": runtime.Version(), "goos": runtime.GOOS, "goarch": runtime.GOARCH,
		"gomaxprocs": runtime.GOMAXPROCS(0), "logical_cpus": runtime.NumCPU(),
		"offered_rps": args.rate, "duration_seconds": args.duration, "offered": offered,
		"measurement_start_unix": unix(start), "measurement_end_unix": unix(observedEnd),
		"nominal_measurement_end_unix": unix(start) + args.duration, "drain_end_unix": unix(drained),
		"measurement_elapsed_seconds": observedEnd.Sub(start).Seconds(),
		"counts":                      counts, "attempted_rps": float64(counts["attempted"]) / args.duration,
		"attempted_scope":       "request tasks admitted; actual wire send rate is not independently measured",
		"goodput_in_window_rps": float64(inWindow) / args.duration, "success_in_window": inWindow,
		"success_after_window": counts["success"] - inWindow, "successful_response_bytes": successfulBytes,
		"drain_seconds": math.Max(0, drained.Sub(end).Seconds()), "error_fraction_of_offered": errorFraction,
		"successful_latency_ms": map[string]any{"p50": percentile(latencies, .5), "p95": percentile(latencies, .95), "p99": p99},
		"latency_scope":         "scheduled monotonic arrival to full-body receipt; mandatory validation follows before success classification",
		"cache":                 cache, "arrival_lag_ms": map[string]any{"max": maxLag, "p99": percentile(lags, .99)},
		"generator_schedule_ok": generatorOK, "slo_pass": pass,
		"thresholds": map[string]any{"p99_ms": args.p99, "error_budget": args.errorBudget, "max_generator_lag_ms": args.maxLag},
		"client": map[string]any{"connections": args.connections, "max_inflight": args.inflight,
			"phase_timeout_seconds": args.phase, "total_http_deadline_seconds": args.deadline, "max_response_body_bytes": args.maxBody,
			"timeout_scope": "Go connect/TLS/header phase timeout; total context deadline covers pool wait/write/full body. No separate body idle or pool timeout; differs from Python HTTPX phases",
			"redirects":     false, "environment_proxy": false, "compression": false, "protocol": "HTTP/1.1"},
		"body_check":    "every 200 UTF-8 body uses UseNumber decode, strict Unicode escapes/EOF and compiled independent expected shape; exact big.Int integer types/values and float64 coordinate semantics; no exact-byte shortcut",
		"warmup_rounds": args.warmup, "examples": examples,
	}, nil
}

func parseOptions(argv []string) (options, error) {
	var args options
	flags := flag.NewFlagSet("http-load", flag.ContinueOnError)
	flags.SetOutput(os.Stderr)
	flags.StringVar(&args.mode, "mode", "open-loop", "open-loop or throughput")
	flags.IntVar(&args.workers, "workers", 0, "Closed-loop workers, one outstanding request each")
	flags.StringVar(&args.expectedCache, "expected-cache", "", "Throughput expected X-Cache: bypass, hit or miss; empty disables cache-header check")
	flags.StringVar(&args.address, "url", "http://api:8000", "HTTP target base URL")
	flags.StringVar(&args.cases, "cases", "", "Independent expected cases JSON")
	flags.Float64Var(&args.rate, "rps", 0, "Offered requests per second")
	flags.Float64Var(&args.duration, "duration", 30, "Measurement seconds")
	flags.IntVar(&args.connections, "connections", 64, "Maximum connections to target host")
	flags.IntVar(&args.inflight, "max-inflight", 256, "Maximum admitted request tasks")
	flags.Float64Var(&args.phase, "timeout", 3, "Go connect/TLS/response-header timeout seconds")
	flags.Float64Var(&args.deadline, "deadline", 5, "Total HTTP deadline seconds")
	flags.IntVar(&args.warmup, "warmup", 2, "Sequential warmup rounds")
	flags.Float64Var(&args.p99, "p99-ms", 200, "p99 SLO milliseconds")
	flags.Float64Var(&args.errorBudget, "error-budget", .001, "Maximum failed/offered fraction")
	flags.Float64Var(&args.maxLag, "max-lag-ms", 50, "Maximum arrival scheduling lag milliseconds")
	flags.Int64Var(&args.maxBody, "max-body-bytes", 8*1024*1024, "Maximum response bytes, explicit new-series client bound")
	flags.IntVar(&args.procs, "gomaxprocs", 1, "Go worker parallelism, zero retains runtime default")
	if err := flags.Parse(argv); err != nil {
		return args, err
	}
	if flags.NArg() != 0 {
		return args, errors.New("unexpected positional arguments")
	}
	used := make(map[string]bool)
	flags.Visit(func(value *flag.Flag) { used[value.Name] = true })
	if args.mode != "open-loop" && args.mode != "throughput" {
		return args, errors.New("unknown mode")
	}
	if args.mode == "throughput" {
		for _, name := range []string{"rps", "connections", "max-inflight", "p99-ms", "error-budget", "max-lag-ms"} {
			if used[name] {
				return args, fmt.Errorf("--%s is not valid in throughput mode", name)
			}
		}
		if args.workers < 1 || args.workers > 4096 {
			return args, errors.New("workers must be within 1..4096")
		}
		if args.expectedCache != "" && args.expectedCache != "bypass" && args.expectedCache != "hit" && args.expectedCache != "miss" {
			return args, errors.New("invalid expected-cache")
		}
	} else if used["workers"] || used["expected-cache"] {
		return args, errors.New("workers/expected-cache require throughput mode")
	}
	finite := func(value float64) bool { return !math.IsNaN(value) && !math.IsInf(value, 0) }
	valid := args.cases != "" && args.connections > 0 && args.inflight > 0 && args.warmup >= 0 && args.procs >= 0 && args.maxBody > 0 && args.maxBody < math.MaxInt64
	for _, value := range []float64{args.rate, args.duration, args.phase, args.deadline, args.p99, args.errorBudget, args.maxLag} {
		valid = valid && finite(value)
	}
	maxSeconds := float64(math.MaxInt64) / float64(time.Second)
	valid = valid && (args.mode == "throughput" || args.rate > 0) && args.duration > 0 && args.duration < maxSeconds && args.phase > 0 && args.phase < maxSeconds && args.deadline > 0 && args.deadline < maxSeconds && args.p99 > 0 && args.errorBudget >= 0 && args.errorBudget <= 1 && args.maxLag >= 0 && args.rate*args.duration < float64(math.MaxInt64)
	if args.mode == "throughput" {
		valid = valid && args.duration >= .001 && args.duration <= 3600 && args.phase <= 60 && args.deadline <= 120 && args.warmup <= 100 && args.maxBody <= 8*1024*1024 && args.procs <= 64
	}
	if !valid {
		return args, errors.New("invalid finite rate/duration/bounds or cases path")
	}
	return args, nil
}

func main() {
	args, err := parseOptions(os.Args[1:])
	if errors.Is(err, flag.ErrHelp) {
		return
	}
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(2)
	}
	result, err := run(args)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	encoder := json.NewEncoder(os.Stdout)
	encoder.SetIndent("", "  ")
	if err := encoder.Encode(result); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
