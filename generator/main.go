// Private full-HTTP generator. Every successful response is verified: fully decoded and checked,
// or byte-identical to a body of the same case that already passed that full check.
package main

import (
	"bytes"
	"context"
	"crypto/sha256"
	"crypto/tls"
	"encoding/hex"
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
	"sync/atomic"
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

// Canonical form for cold-case verification, hashed with SHA-256:
// the body is decoded with decodeJSON (UseNumber, strict Unicode escapes/EOF, duplicate keys rejected),
// then serialized recursively without whitespace:
//   - null, true, false as literals;
//   - strings as encoding/json Marshal of the Go string;
//   - arrays "[a,b]" in order;
//   - objects "{" + keys sorted bytewise, each marshaled key + ":" + value, comma separated + "}";
//   - integer number tokens (no . e E) as big.Int decimal;
//   - other number tokens parsed as float64 (NaN, Inf and parse errors rejected); integral values
//     with |x| <= 2^53 written as integer decimal, else strconv.FormatFloat(x, 'g', -1, 64).
//
// This intentionally treats 55 and 55.0 (and -0, 0.0) as equal. Integral floats above 2^53 keep the
// 'g' form, so they differ from the same value written as an integer token.
func canonicalJSON(raw []byte) ([]byte, error) {
	decoded, err := decodeJSON(raw)
	if err != nil {
		return nil, err
	}
	var buffer bytes.Buffer
	if err := writeCanonical(&buffer, decoded); err != nil {
		return nil, err
	}
	return buffer.Bytes(), nil
}

func canonicalSHA256(raw []byte) ([32]byte, error) {
	canonical, err := canonicalJSON(raw)
	if err != nil {
		return [32]byte{}, err
	}
	return sha256.Sum256(canonical), nil
}

func writeCanonical(buffer *bytes.Buffer, value any) error {
	switch v := value.(type) {
	case nil:
		buffer.WriteString("null")
	case bool:
		buffer.WriteString(strconv.FormatBool(v))
	case string:
		encoded, err := json.Marshal(v)
		if err != nil {
			return err
		}
		buffer.Write(encoded)
	case json.Number:
		if integerToken(v) {
			integer, ok := new(big.Int).SetString(string(v), 10)
			if !ok {
				return errors.New("invalid integer token")
			}
			buffer.WriteString(integer.String())
			return nil
		}
		floating, err := strconv.ParseFloat(string(v), 64)
		if err != nil || math.IsNaN(floating) || math.IsInf(floating, 0) {
			return errors.New("nonfinite or invalid float token")
		}
		if math.Trunc(floating) == floating && math.Abs(floating) <= 1<<53 {
			buffer.WriteString(strconv.FormatInt(int64(floating), 10))
		} else {
			buffer.WriteString(strconv.FormatFloat(floating, 'g', -1, 64))
		}
	case []any:
		buffer.WriteByte('[')
		for index, item := range v {
			if index > 0 {
				buffer.WriteByte(',')
			}
			if err := writeCanonical(buffer, item); err != nil {
				return err
			}
		}
		buffer.WriteByte(']')
	case map[string]any:
		keys := make([]string, 0, len(v))
		for key := range v {
			keys = append(keys, key)
		}
		sort.Strings(keys)
		buffer.WriteByte('{')
		for index, key := range keys {
			if index > 0 {
				buffer.WriteByte(',')
			}
			encoded, err := json.Marshal(key)
			if err != nil {
				return err
			}
			buffer.Write(encoded)
			buffer.WriteByte(':')
			if err := writeCanonical(buffer, v[key]); err != nil {
				return err
			}
		}
		buffer.WriteByte('}')
	default:
		return errors.New("unsupported decoded JSON value")
	}
	return nil
}

// Per-case raw bodies that already passed the full check in this process. Readers load an
// immutable slice; writers copy on write under the mutex. A nil set disables the shortcut.
const verifiedBodiesCap = 8

type verifiedBodies struct {
	mutex  sync.Mutex
	bodies atomic.Pointer[[][]byte]
}

func (set *verifiedBodies) contains(raw []byte) bool {
	if set == nil {
		return false
	}
	current := set.bodies.Load()
	if current == nil {
		return false
	}
	for _, body := range *current {
		if len(body) == len(raw) && bytes.Equal(body, raw) {
			return true
		}
	}
	return false
}

// raw must not be modified afterwards; io.ReadAll results are not reused.
func (set *verifiedBodies) add(raw []byte) {
	if set == nil {
		return
	}
	set.mutex.Lock()
	defer set.mutex.Unlock()
	var current [][]byte
	if loaded := set.bodies.Load(); loaded != nil {
		current = *loaded
	}
	if len(current) >= verifiedBodiesCap {
		return
	}
	for _, body := range current {
		if bytes.Equal(body, raw) {
			return
		}
	}
	next := make([][]byte, len(current), len(current)+1)
	copy(next, current)
	next = append(next, raw)
	set.bodies.Store(&next)
}

type fileCase struct {
	Path     string            `json:"path"`
	Params   map[string]string `json:"params"`
	Method   string            `json:"method"`
	Body     json.RawMessage   `json:"body"`
	Expected json.RawMessage   `json:"expected"`
}

// Cold cases are GET only and carry a canonical SHA-256 instead of an expected body.
type coldFileCase struct {
	Path            string            `json:"path"`
	Params          map[string]string `json:"params,omitempty"`
	CanonicalSHA256 string            `json:"canonical_sha256"`
}

type loadCase struct {
	path      string
	address   string
	expected  *expectedValue
	method    string // empty means GET
	body      []byte // compact POST body
	verified  *verifiedBodies
	cold      bool
	canonical [32]byte
}

// Strict JSON list file: decodeJSON validity, unknown fields rejected, no trailing data.
func readCaseFile(path string, target any) error {
	raw, err := os.ReadFile(path)
	if err != nil {
		return err
	}
	if _, err := decodeJSON(raw); err != nil {
		return fmt.Errorf("invalid cases JSON: %w", err)
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(target); err != nil {
		return err
	}
	var trailing any
	if err := decoder.Decode(&trailing); err != io.EOF {
		return errors.New("trailing cases JSON")
	}
	return nil
}

func parseBase(baseAddress string) (*url.URL, error) {
	base, err := url.Parse(baseAddress)
	if err != nil || base.Host == "" || (base.Scheme != "http" && base.Scheme != "https") || base.User != nil {
		return nil, errors.New("need an HTTP(S) URL without userinfo")
	}
	return base, nil
}

func caseAddress(base *url.URL, path string, params map[string]string) (string, error) {
	reference, err := url.Parse(path)
	if err != nil || path == "" || reference.IsAbs() || reference.Host != "" {
		return "", errors.New("cases need relative paths on the selected target")
	}
	address := base.ResolveReference(reference)
	query := address.Query()
	for key, value := range params {
		query.Set(key, value)
	}
	address.RawQuery = query.Encode()
	return address.String(), nil
}

func loadCases(path, baseAddress string) ([]loadCase, error) {
	var files []fileCase
	if err := readCaseFile(path, &files); err != nil {
		return nil, err
	}
	if len(files) == 0 {
		return nil, errors.New("cases must be nonempty")
	}
	base, err := parseBase(baseAddress)
	if err != nil {
		return nil, err
	}
	result := make([]loadCase, len(files))
	for index, file := range files {
		address, err := caseAddress(base, file.Path, file.Params)
		if err != nil {
			return nil, err
		}
		item := loadCase{path: file.Path, address: address, method: http.MethodGet, verified: &verifiedBodies{}}
		switch file.Method {
		case "", http.MethodGet:
			if len(file.Body) != 0 {
				return nil, fmt.Errorf("case %s: GET case must not have a body", file.Path)
			}
		case http.MethodPost:
			if len(file.Body) == 0 {
				return nil, fmt.Errorf("case %s: POST case needs a body", file.Path)
			}
			if _, err := decodeJSON(file.Body); err != nil {
				return nil, fmt.Errorf("case %s body: %w", file.Path, err)
			}
			// Removes insignificant whitespace only; tokens and escapes keep their file bytes.
			var compact bytes.Buffer
			if err := json.Compact(&compact, file.Body); err != nil {
				return nil, fmt.Errorf("case %s body: %w", file.Path, err)
			}
			item.method, item.body = http.MethodPost, compact.Bytes()
		default:
			return nil, fmt.Errorf("case %s: method must be GET or POST", file.Path)
		}
		decoded, err := decodeJSON(file.Expected)
		if err != nil {
			return nil, fmt.Errorf("case %s: %w", file.Path, err)
		}
		if item.expected, err = compileExpected(decoded); err != nil {
			return nil, err
		}
		result[index] = item
	}
	return result, nil
}

// Hashes are lowercase hex; record mode accepts them empty and replaces them.
func loadColdCases(path, baseAddress string, recording bool) ([]loadCase, []coldFileCase, error) {
	var files []coldFileCase
	if err := readCaseFile(path, &files); err != nil {
		return nil, nil, err
	}
	if len(files) == 0 {
		return nil, nil, errors.New("cold cases must be nonempty")
	}
	base, err := parseBase(baseAddress)
	if err != nil {
		return nil, nil, err
	}
	result := make([]loadCase, len(files))
	for index, file := range files {
		address, err := caseAddress(base, file.Path, file.Params)
		if err != nil {
			return nil, nil, err
		}
		item := loadCase{path: file.Path, address: address, method: http.MethodGet, cold: true}
		if !recording || file.CanonicalSHA256 != "" {
			digest, err := hex.DecodeString(file.CanonicalSHA256)
			if err != nil || len(digest) != sha256.Size || strings.ToLower(file.CanonicalSHA256) != file.CanonicalSHA256 {
				return nil, nil, fmt.Errorf("cold case %d %s: canonical_sha256 must be 64 lowercase hex digits", index, file.Path)
			}
			copy(item.canonical[:], digest)
		}
		result[index] = item
	}
	return result, files, nil
}

// Cold-key mix: the measured request with counter value c uses a cold case when
// c % every == every-1, otherwise the next hot case. Cold cases are consumed strictly in
// order from offset, each at most once per process; an exhausted pool never wraps.
type requestMix struct {
	every, offset int64
	cold          []loadCase
	requests      atomic.Int64
	taken         atomic.Int64
}

var coldPoolExhausted = outcome{category: "client_errors", detail: "cold pool exhausted", cold: true}

// pick returns the case for the next measured request; ok is false when the cold pool is exhausted.
func (mix *requestMix) pick(nextHot func() loadCase) (loadCase, bool) {
	if mix == nil {
		return nextHot(), true
	}
	if mix.requests.Add(1)%mix.every != 0 {
		return nextHot(), true
	}
	index := mix.offset + mix.taken.Add(1) - 1
	if index >= int64(len(mix.cold)) {
		return loadCase{cold: true}, false
	}
	return mix.cold[index], true
}

func (mix *requestMix) report(result map[string]any) {
	if mix == nil {
		return
	}
	taken := mix.taken.Load()
	consumed := min(taken, int64(len(mix.cold))-mix.offset)
	var used any
	if consumed > 0 {
		used = mix.offset + consumed - 1
	}
	result["cold_every"] = mix.every
	result["cold_offset"] = mix.offset
	result["cold_pool_size"] = len(mix.cold)
	result["cold_consumed"] = consumed
	result["cold_used"] = used
	result["cold_exhausted"] = taken - consumed
	result["cold_scope"] = "measured requests only, warmup is hot only; cold_used is the highest cold index consumed (null if none); the next run may start at cold_used+1"
}

func newMix(args options) (*requestMix, error) {
	if args.coldEvery == 0 {
		return nil, nil
	}
	cold, _, err := loadColdCases(args.coldCases, args.address, false)
	if err != nil {
		return nil, fmt.Errorf("cold cases: %w", err)
	}
	if args.coldOffset >= len(cold) {
		return nil, fmt.Errorf("cold-offset %d is outside the %d cold cases", args.coldOffset, len(cold))
	}
	return &requestMix{every: int64(args.coldEvery), offset: int64(args.coldOffset), cold: cold}, nil
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
	coldCases     string
	coldEvery     int
	coldOffset    int
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
	checkElapsed time.Duration
	category     string
	received     time.Time
	bytes        int
	cache        string
	status       int
	media        string
	detail       string
	check        string // "full", "memo" or "cold" once a 200 body was verified
	cold         bool
	digest       [32]byte // record mode canonical SHA-256
}

func newRequest(ctx context.Context, item loadCase) (*http.Request, error) {
	if item.method != http.MethodPost {
		return http.NewRequestWithContext(ctx, http.MethodGet, item.address, nil)
	}
	request, err := http.NewRequestWithContext(ctx, http.MethodPost, item.address, bytes.NewReader(item.body))
	if err == nil {
		request.Header.Set("Content-Type", "application/json")
	}
	return request, err
}

// verify classifies a 200 body. Hot cases: a body byte-identical to one that already passed the
// full check for the same case is accepted without decoding, which is equivalent to checking it
// again; any other body gets the full check and joins the case's verified set on success.
// Cold cases always compare the canonical SHA-256; recording only computes it.
func (item loadCase) verify(raw []byte, recording bool) (category, check, detail string, digest [32]byte) {
	if item.cold {
		digest, err := canonicalSHA256(raw)
		if err != nil {
			return "client_errors", "cold", "invalid response JSON: " + err.Error(), digest
		}
		if !recording && digest != item.canonical {
			return "wrong_body", "cold", "", digest
		}
		return "success", "cold", "", digest
	}
	if item.verified.contains(raw) {
		return "success", "memo", "", digest
	}
	decoded, err := decodeJSON(raw)
	if err != nil {
		return "client_errors", "full", "invalid response JSON: " + err.Error(), digest
	}
	if !item.expected.matches(decoded) {
		return "wrong_body", "full", "", digest
	}
	item.verified.add(raw)
	return "success", "full", "", digest
}

func execute(client *http.Client, item loadCase, args options) (result outcome) {
	defer func() { result.cold = item.cold }()
	ctx, cancel := context.WithTimeout(context.Background(), time.Duration(args.deadline*float64(time.Second)))
	defer cancel()
	request, err := newRequest(ctx, item)
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
	checkStarted := time.Now()
	defer func() { result.checkElapsed = time.Since(checkStarted) }()
	category, check, detail, digest := item.verify(raw, args.mode == "record")
	if category != "success" {
		return outcome{category: category, status: 200, cache: cache, media: media, detail: detail, check: check}
	}
	if args.mode == "throughput" || args.mode == "record" {
		mediaType, parameters, err := mime.ParseMediaType(media)
		charset, hasCharset := parameters["charset"]
		if err != nil || len(response.Header.Values("Content-Type")) != 1 || mediaType != "application/json" || (hasCharset && !strings.EqualFold(charset, "utf-8")) {
			return outcome{category: "bad_headers", status: 200, media: media, cache: cache, detail: "expected UTF-8 application/json Content-Type", check: check}
		}
		// A cold key is requested once per process on a flushed cache, so under hit-or-miss it must miss.
		wantCache, detail := args.expectedCache, "unexpected X-Cache header"
		if item.cold && wantCache == "hit-or-miss" {
			wantCache, detail = "miss", "cold request needs exactly one X-Cache: miss"
		}
		cacheMatches := cache == wantCache || (wantCache == "hit-or-miss" && (cache == "hit" || cache == "miss"))
		if wantCache != "" && (!cacheMatches || len(response.Header.Values("X-Cache")) != 1) {
			return outcome{category: "bad_headers", status: 200, media: media, cache: cache, detail: detail, check: check}
		}
	}
	return outcome{category: "success", received: received, bytes: len(raw), cache: cache, status: 200, media: media, check: check, digest: digest}
}

// Successful responses per X-Cache value, split by hot and cold case.
type cacheByKind struct {
	hot, cold map[string]int64
}

func (counts *cacheByKind) add(result outcome) {
	if counts.hot == nil {
		counts.hot, counts.cold = make(map[string]int64), make(map[string]int64)
	}
	if result.cold {
		counts.cold[result.cache]++
	} else {
		counts.hot[result.cache]++
	}
}

func (counts *cacheByKind) merge(other cacheByKind) {
	if counts.hot == nil {
		counts.hot, counts.cold = make(map[string]int64), make(map[string]int64)
	}
	for key, value := range other.hot {
		counts.hot[key] += value
	}
	for key, value := range other.cold {
		counts.cold[key] += value
	}
}

func (counts cacheByKind) report(mix bool) map[string]map[string]int64 {
	result := map[string]map[string]int64{"hot": counts.hot}
	if result["hot"] == nil {
		result["hot"] = map[string]int64{}
	}
	if mix {
		result["cold"] = counts.cold
		if result["cold"] == nil {
			result["cold"] = map[string]int64{}
		}
	}
	return result
}

// Verification and cold/hot counters; every outcome counts as one request.
type checkCounts struct {
	full, memo, cold, coldRequests, hotRequests int64
}

func (counts *checkCounts) add(result outcome) {
	switch result.check {
	case "full":
		counts.full++
	case "memo":
		counts.memo++
	case "cold":
		counts.cold++
	}
	if result.cold {
		counts.coldRequests++
	} else {
		counts.hotRequests++
	}
}

func (counts *checkCounts) merge(other checkCounts) {
	counts.full += other.full
	counts.memo += other.memo
	counts.cold += other.cold
	counts.coldRequests += other.coldRequests
	counts.hotRequests += other.hotRequests
}

func (counts checkCounts) fields(result map[string]any, suffix string, mix bool) {
	result["full_checks"+suffix] = counts.full
	result["memo_hits"+suffix] = counts.memo
	if mix {
		result["cold_checks"+suffix] = counts.cold
		result["cold_requests"+suffix] = counts.coldRequests
		result["hot_requests"+suffix] = counts.hotRequests
	}
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
	mix, err := newMix(args)
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
		return runThroughput(args, cases, client, mix), nil
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
	var checks, checksInWindow checkCounts
	var cacheInWindow cacheByKind
	var hotIndex int64
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
			item, ok := cases[index%int64(len(cases))], true
			if mix != nil {
				item, ok = mix.pick(func() loadCase {
					hotIndex++
					return cases[(hotIndex-1)%int64(len(cases))]
				})
			}
			workers.Add(1)
			go func(item loadCase, ok bool, scheduled time.Time) {
				defer workers.Done()
				defer func() { <-slots }()
				result := coldPoolExhausted
				if ok {
					result = execute(client, item, args)
				}
				completed := time.Now()
				mutex.Lock()
				defer mutex.Unlock()
				counts[result.category]++
				checks.add(result)
				if completed.Before(end) {
					checksInWindow.add(result)
				}
				if result.category == "success" {
					if result.received.Before(end) {
						inWindow++
						cacheInWindow.add(result)
					}
					successfulBytes += int64(result.bytes)
					cache[result.cache]++
					latencies = append(latencies, float64(result.received.Sub(scheduled))/float64(time.Millisecond))
				} else if len(examples) < 3 {
					examples = append(examples, map[string]any{"path": item.path, "category": result.category, "status": result.status, "error": result.detail})
				}
			}(item, ok, scheduled)
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
	result := map[string]any{
		"kind": "full-http-read-pilot", "claim_capacity": false,
		"generator":  "Go standard-library HTTP/1.1, all 200 bodies verified (full check or byte-identical to a fully checked body of the same case)",
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
		"body_check":    bodyCheck,
		"warmup_rounds": args.warmup, "examples": examples,
		"check_counters_scope": "measured requests; *_in_window by completion after checking before nominal end; warmup excluded",
	}
	checks.fields(result, "", mix != nil)
	checksInWindow.fields(result, "_in_window", mix != nil)
	result["cache_by_kind_in_window"] = cacheInWindow.report(mix != nil)
	mix.report(result)
	return result, nil
}

const bodyCheck = "every 200 body of a hot case is byte-compared with up to 8 bodies of the same case that already passed the full check in this process; " +
	"an identical body is accepted without decoding (memo_hits), equivalent to checking it again; any other body gets the full check (full_checks): " +
	"UTF-8, strict Unicode escapes/EOF, duplicate decoded keys rejected, UseNumber decode against the compiled independent expected shape with exact big.Int integer types/values and float64 coordinate semantics, then memoized on success; " +
	"cold cases (cold_checks) compare the SHA-256 of the canonical form of the strictly decoded body, never memoized"

// runRecord requests every cold case once and returns the cases with canonical_sha256 filled in.
// Any non-200, non-JSON Content-Type or decode failure fails the whole recording.
func runRecord(args options) ([]coldFileCase, error) {
	items, files, err := loadColdCases(args.coldCases, args.address, true)
	if err != nil {
		return nil, err
	}
	if args.procs > 0 {
		runtime.GOMAXPROCS(args.procs)
	}
	args.connections = args.workers
	client := clientFor(args)
	defer client.CloseIdleConnections()
	failures := make([]error, len(items))
	var next atomic.Int64
	var failed atomic.Bool
	var workers sync.WaitGroup
	for range args.workers {
		workers.Add(1)
		go func() {
			defer workers.Done()
			for !failed.Load() {
				index := next.Add(1) - 1
				if index >= int64(len(items)) {
					return
				}
				result := execute(client, items[index], args)
				if result.category != "success" {
					failures[index] = fmt.Errorf("record cold case %d %s: %s status %d content-type %q %s", index, items[index].path, result.category, result.status, result.media, result.detail)
					failed.Store(true)
					continue
				}
				files[index].CanonicalSHA256 = hex.EncodeToString(result.digest[:])
			}
		}()
	}
	workers.Wait()
	for _, err := range failures {
		if err != nil {
			return nil, err
		}
	}
	return files, nil
}

func parseOptions(argv []string) (options, error) {
	var args options
	flags := flag.NewFlagSet("http-load", flag.ContinueOnError)
	flags.SetOutput(os.Stderr)
	flags.StringVar(&args.mode, "mode", "open-loop", "open-loop, throughput or record")
	flags.IntVar(&args.workers, "workers", 0, "Closed-loop workers, one outstanding request each; record mode default 4")
	flags.StringVar(&args.expectedCache, "expected-cache", "", "Throughput expected X-Cache: bypass, hit, miss or hit-or-miss; empty disables cache-header check")
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
	flags.StringVar(&args.coldCases, "cold-cases", "", "Cold cases JSON: list of {path, params, canonical_sha256}")
	flags.IntVar(&args.coldEvery, "cold-every", 0, "Every k-th measured request uses the next cold case; 0 disables, else >= 2")
	flags.IntVar(&args.coldOffset, "cold-offset", 0, "First cold case index; each cold case is used at most once")
	if err := flags.Parse(argv); err != nil {
		return args, err
	}
	if flags.NArg() != 0 {
		return args, errors.New("unexpected positional arguments")
	}
	used := make(map[string]bool)
	flags.Visit(func(value *flag.Flag) { used[value.Name] = true })
	if args.mode != "open-loop" && args.mode != "throughput" && args.mode != "record" {
		return args, errors.New("unknown mode")
	}
	if args.mode == "record" {
		for _, name := range []string{"cases", "rps", "duration", "warmup", "connections", "max-inflight", "p99-ms", "error-budget", "max-lag-ms", "expected-cache", "cold-every", "cold-offset"} {
			if used[name] {
				return args, fmt.Errorf("--%s is not valid in record mode", name)
			}
		}
		if !used["workers"] {
			args.workers = 4
		}
		if args.workers < 1 || args.workers > 4096 {
			return args, errors.New("workers must be within 1..4096")
		}
		if args.coldCases == "" {
			return args, errors.New("record mode needs --cold-cases")
		}
	} else if args.coldEvery < 0 || args.coldEvery == 1 || args.coldOffset < 0 {
		return args, errors.New("cold-every must be 0 or >= 2 and cold-offset >= 0")
	} else if (args.coldEvery > 0) != (args.coldCases != "") || (used["cold-offset"] && args.coldEvery == 0) {
		return args, errors.New("cold-every > 0 and cold-cases require each other; cold-offset requires cold-every")
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
		if args.expectedCache != "" && args.expectedCache != "bypass" && args.expectedCache != "hit" && args.expectedCache != "miss" && args.expectedCache != "hit-or-miss" {
			return args, errors.New("invalid expected-cache")
		}
	} else if args.mode == "open-loop" && (used["workers"] || used["expected-cache"]) {
		return args, errors.New("workers/expected-cache require throughput mode")
	}
	finite := func(value float64) bool { return !math.IsNaN(value) && !math.IsInf(value, 0) }
	valid := (args.mode == "record" || args.cases != "") && args.connections > 0 && args.inflight > 0 && args.warmup >= 0 && args.procs >= 0 && args.maxBody > 0 && args.maxBody < math.MaxInt64
	for _, value := range []float64{args.rate, args.duration, args.phase, args.deadline, args.p99, args.errorBudget, args.maxLag} {
		valid = valid && finite(value)
	}
	maxSeconds := float64(math.MaxInt64) / float64(time.Second)
	valid = valid && (args.mode != "open-loop" || args.rate > 0) && args.duration > 0 && args.duration < maxSeconds && args.phase > 0 && args.phase < maxSeconds && args.deadline > 0 && args.deadline < maxSeconds && args.p99 > 0 && args.errorBudget >= 0 && args.errorBudget <= 1 && args.maxLag >= 0 && args.rate*args.duration < float64(math.MaxInt64)
	if args.mode == "record" {
		valid = valid && args.phase <= 60 && args.deadline <= 120 && args.maxBody <= 8*1024*1024 && args.procs <= 64
	}
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
	var result any
	if args.mode == "record" {
		result, err = runRecord(args)
	} else {
		result, err = run(args)
	}
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
