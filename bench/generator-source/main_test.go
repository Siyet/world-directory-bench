package main

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func checker(t *testing.T, raw string) *expectedValue {
	t.Helper()
	decoded, err := decodeJSON([]byte(raw))
	if err != nil {
		t.Fatal(err)
	}
	checked, err := compileExpected(decoded)
	if err != nil {
		t.Fatal(err)
	}
	return checked
}

func TestStrictJSONSemantics(t *testing.T) {
	cases := []struct {
		name, expected, actual string
		want                   bool
	}{
		{"int64 exact", `{"n":9223372036854775807}`, `{"n":9223372036854775807}`, true},
		{"int64 wrong", `{"n":9223372036854775807}`, `{"n":9223372036854775806}`, false},
		{"large exact", `{"n":9007199254740993}`, `{"n":9007199254740993}`, true},
		{"integer rejects float", `{"n":1}`, `{"n":1.0}`, false},
		{"integer rejects exponent", `{"n":1}`, `{"n":1e0}`, false},
		{"integer rejects bool", `{"n":1}`, `{"n":true}`, false},
		{"bool rejects integer", `{"n":true}`, `{"n":1}`, false},
		{"coordinate accepts integer", `{"n":1.0}`, `{"n":1}`, true},
		{"coordinate binary rounding", `{"n":1.0}`, `{"n":1.0000000000000001}`, true},
		{"float integer exactness", `{"n":9007199254740992.0}`, `{"n":9007199254740993}`, false},
		{"float integer exact match", `{"n":9007199254740992.0}`, `{"n":9007199254740992}`, true},
		{"negative zero", `{"n":0.0}`, `{"n":-0}`, true},
		{"coordinate rejects bool", `{"n":1.0}`, `{"n":true}`, false},
		{"missing nullable key", `{"n":null}`, `{"other":null}`, false},
		{"extra nested", `{"n":{"x":null}}`, `{"n":{"x":null,"extra":1}}`, false},
		{"missing nested", `{"n":{"x":null}}`, `{"n":{}}`, false},
		{"array types", `{"n":[1,true,null]}`, `{"n":[1,1,null]}`, false},
		{"array length", `{"n":[1]}`, `{"n":[1,2]}`, false},
		{"literal escaped backslash", `{"n":"\\ud800"}`, `{"n":"\\ud800"}`, true},
		{"surrogate pair", `{"n":"😀"}`, `{"n":"\ud83d\ude00"}`, true},
		{"nonfinite float", `{"n":0.0}`, `{"n":1e999}`, false},
		{"underflow float", `{"n":0.0}`, `{"n":1e-999}`, true},
		{"key order", `{"a":1,"b":null}`, `{"b":null,"a":1}`, true},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			actual, err := decodeJSON([]byte(c.actual))
			if err != nil {
				t.Fatal(err)
			}
			if got := checker(t, c.expected).matches(actual); got != c.want {
				t.Fatalf("matches=%v want=%v", got, c.want)
			}
		})
	}
}

func TestInvalidUnicodeSyntaxAndTrailingJSON(t *testing.T) {
	bodies := [][]byte{[]byte(`{"n":"\ud800"}`), []byte(`{"n":"\udc00"}`), []byte(`{"n":"\ud800\ud800"}`), []byte(`{"n":"\ud800\\udc00"}`), []byte(`{"n":"\uZZZZ"}`), []byte(`{"n":1} {"n":1}`), []byte(`{"n":1}x`), []byte(`{"n":NaN}`), []byte(`{"n":1`), {'{', '"', 'n', '"', ':', '"', 255, '"', '}'}, append([]byte{239, 187, 191}, []byte(`{"n":1}`)...)}
	for _, raw := range bodies {
		if _, err := decodeJSON(raw); err == nil {
			t.Fatalf("accepted %q", raw)
		}
	}
	replacement := checker(t, `{"n":"�"}`)
	for _, raw := range []string{`{"n":"\ud800"}`, `{"n":"\udc00"}`} {
		actual, err := decodeJSON([]byte(raw))
		if err == nil && replacement.matches(actual) {
			t.Fatal("replacement accepted malformed surrogate")
		}
	}
	if _, err := compileExpected(json.Number("1e999")); err == nil {
		t.Fatal("nonfinite expectation")
	}
}

func TestHTTPStatusAndTimeoutClassification(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/ok", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("X-Cache", "bypass")
		io.WriteString(w, `{"n":1}`)
	})
	mux.HandleFunc("/wrong", func(w http.ResponseWriter, r *http.Request) { io.WriteString(w, `{"n":true}`) })
	mux.HandleFunc("/invalid", func(w http.ResponseWriter, r *http.Request) { io.WriteString(w, `{"n":"\ud800"}`) })
	mux.HandleFunc("/redirect", func(w http.ResponseWriter, r *http.Request) { http.Redirect(w, r, "/ok", 302) })
	mux.HandleFunc("/header", func(w http.ResponseWriter, r *http.Request) {
		time.Sleep(80 * time.Millisecond)
		io.WriteString(w, `{"n":1}`)
	})
	mux.HandleFunc("/body", func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(200)
		w.(http.Flusher).Flush()
		time.Sleep(80 * time.Millisecond)
		io.WriteString(w, `{"n":1}`)
	})
	server := httptest.NewServer(mux)
	defer server.Close()
	args := options{connections: 1, inflight: 1, phase: .03, deadline: .05, maxBody: 1024}
	client := clientFor(args)
	defer client.CloseIdleConnections()
	expected := checker(t, `{"n":1}`)
	for _, item := range []struct{ path, category string }{{"/ok", "success"}, {"/wrong", "wrong_body"}, {"/invalid", "client_errors"}, {"/redirect", "http_errors"}, {"/header", "timeouts"}, {"/body", "deadline_timeouts"}} {
		result := execute(client, loadCase{item.path, server.URL + item.path, expected}, args)
		if result.category != item.category {
			t.Fatalf("%s: category %s detail %s", item.path, result.category, result.detail)
		}
		if item.path == "/ok" && (result.cache != "bypass" || result.received.IsZero()) {
			t.Fatal(result)
		}
	}
}

func TestBoundedOpenLoopOffers(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		time.Sleep(15 * time.Millisecond)
		io.WriteString(w, `{"n":1}`)
	}))
	defer server.Close()
	path := filepath.Join(t.TempDir(), "cases.json")
	if err := os.WriteFile(path, []byte(`[{"path":"/","expected":{"n":1}}]`), 0600); err != nil {
		t.Fatal(err)
	}
	result, err := run(options{address: server.URL, cases: path, rate: 1000, duration: .06, connections: 1, inflight: 1, phase: .2, deadline: .3, maxBody: 1024, procs: 1, p99: 200, errorBudget: 0, maxLag: 50})
	if err != nil {
		t.Fatal(err)
	}
	counts := result["counts"].(map[string]int64)
	if counts["attempted"]+counts["drops"] != result["offered"].(int64) || counts["drops"] == 0 || counts["success"] != counts["attempted"] || result["slo_pass"].(bool) {
		t.Fatal(result)
	}
	if result["success_in_window"].(int64)+result["success_after_window"].(int64) != counts["success"] {
		t.Fatal(result)
	}
}
