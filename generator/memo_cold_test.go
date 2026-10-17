package main

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
)

func canonicalHex(canonical string) string {
	sum := sha256.Sum256([]byte(canonical))
	return hex.EncodeToString(sum[:])
}

func TestMemoShortcutAndFullCheck(t *testing.T) {
	var body atomic.Value
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		jsonResponse(w, body.Load().(string))
	}))
	defer server.Close()
	cases, err := loadCases(caseFile(t, `[{"path":"/","expected":{"n":1,"s":"x"}}]`), server.URL)
	if err != nil {
		t.Fatal(err)
	}
	args := throughputArgs(t, server.URL, 1)
	client := clientFor(options{connections: 1, phase: .2, deadline: .3})
	defer client.CloseIdleConnections()
	step := func(raw, category, check string) {
		t.Helper()
		body.Store(raw)
		got := execute(client, cases[0], args)
		if got.category != category || got.check != check {
			t.Fatalf("%s: got %s/%s want %s/%s %s", raw, got.category, got.check, category, check, got.detail)
		}
	}
	step(`{"n":1,"s":"x"}`, "success", "full")
	step(`{"n":1,"s":"x"}`, "success", "memo")
	step(`{"n":2,"s":"x"}`, "wrong_body", "full")
	step(`{"n":2,"s":"x"}`, "wrong_body", "full")
	step(`{"n":1,"s":"x","n":1}`, "client_errors", "full")
	step(`{"s":"x","n":1}`, "success", "full")
	step(`{"s":"x","n":1}`, "success", "memo")
	step(`{"n":1,"s":"\u0078"}`, "success", "full")
	step(`{"n":1,"s":"\u0078"}`, "success", "memo")
	step(`{"n":1,"s":"x"}`, "success", "memo")
	// Fill the per-case cap of 8 verified bodies; later variants keep using the full check.
	for spaces := 1; spaces <= 5; spaces++ {
		step(`{"n":1,`+strings.Repeat(" ", spaces)+`"s":"x"}`, "success", "full")
	}
	step(`{"n":1, "s":"x"}`, "success", "memo")
	over := `{"n":1,      "s":"x"}`
	step(over, "success", "full")
	step(over, "success", "full")
	if got := len(*cases[0].verified.bodies.Load()); got != verifiedBodiesCap {
		t.Fatal("verified set size", got)
	}
}

func TestMemoIsPerCase(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { jsonResponse(w, `{"n":1}`) }))
	defer server.Close()
	cases, err := loadCases(caseFile(t, `[{"path":"/a","expected":{"n":1}},{"path":"/b","expected":{"n":2}}]`), server.URL)
	if err != nil {
		t.Fatal(err)
	}
	args := throughputArgs(t, server.URL, 1)
	client := clientFor(options{connections: 1, phase: .2, deadline: .3})
	defer client.CloseIdleConnections()
	for _, want := range []struct {
		index           int
		category, check string
	}{{0, "success", "full"}, {0, "success", "memo"}, {1, "wrong_body", "full"}, {1, "wrong_body", "full"}, {0, "success", "memo"}} {
		got := execute(client, cases[want.index], args)
		if got.category != want.category || got.check != want.check {
			t.Fatal(want, got)
		}
	}
}

func TestMemoConcurrentVerify(t *testing.T) {
	cases, err := loadCases(caseFile(t, `[{"path":"/","expected":{"n":1}}]`), "http://localhost")
	if err != nil {
		t.Fatal(err)
	}
	var group sync.WaitGroup
	var full, memo atomic.Int64
	for worker := range 16 {
		group.Add(1)
		go func() {
			defer group.Done()
			for i := range 200 {
				raw := []byte(`{"n":1` + strings.Repeat(" ", (worker+i)%12) + `}`)
				category, check, _, _ := cases[0].verify(raw, false)
				if category != "success" {
					t.Error(category)
					return
				}
				if check == "memo" {
					memo.Add(1)
				} else {
					full.Add(1)
				}
			}
		}()
	}
	group.Wait()
	if memo.Load() == 0 || len(*cases[0].verified.bodies.Load()) != verifiedBodiesCap || full.Load()+memo.Load() != 3200 {
		t.Fatal(full.Load(), memo.Load())
	}
}

func TestPostCasesSendExactBody(t *testing.T) {
	type seen struct{ method, media, body string }
	var mutex sync.Mutex
	var requests []seen
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		raw, _ := io.ReadAll(r.Body)
		mutex.Lock()
		requests = append(requests, seen{r.Method, r.Header.Get("Content-Type"), string(raw)})
		mutex.Unlock()
		jsonResponse(w, `{"n":1}`)
	}))
	defer server.Close()
	file := caseFile(t, `[{"path":"/p","method":"POST","body":{"q": [1, 2.50, "\u00e9", 1E2]},"expected":{"n":1}},
		{"path":"/g","method":"GET","expected":{"n":1}}]`)
	args := throughputArgs(t, server.URL, 1)
	args.cases = file
	args.warmup = 1
	result, err := run(args)
	if err != nil {
		t.Fatal(err)
	}
	if result["in_window"].(map[string]any)["errors"].(int64) != 0 {
		t.Fatal(result)
	}
	mutex.Lock()
	defer mutex.Unlock()
	var posts, gets int
	for _, request := range requests {
		switch request.method {
		case "POST":
			posts++
			if request.media != "application/json" || request.body != `{"q":[1,2.50,"\u00e9",1E2]}` {
				t.Fatalf("%+v", request)
			}
		case "GET":
			gets++
			if request.body != "" || request.media != "" {
				t.Fatalf("%+v", request)
			}
		default:
			t.Fatal(request)
		}
	}
	if posts == 0 || gets == 0 {
		t.Fatal(posts, gets)
	}
}

func TestCaseMethodAndBodyValidation(t *testing.T) {
	for _, raw := range []string{
		`[{"path":"/","body":{"a":1},"expected":1}]`,
		`[{"path":"/","method":"GET","body":null,"expected":1}]`,
		`[{"path":"/","method":"POST","expected":1}]`,
		`[{"path":"/","method":"post","body":{},"expected":1}]`,
		`[{"path":"/","method":"PUT","body":{},"expected":1}]`,
		`[{"path":"/","method":"POST","body":{"a":1,"a":2},"expected":1}]`,
		`[{"path":"/","method":"POST","body":{},"expected":1,"headers":{}}]`,
	} {
		if _, err := loadCases(caseFile(t, raw), "http://localhost"); err == nil {
			t.Fatalf("accepted %s", raw)
		}
	}
	cases, err := loadCases(caseFile(t, `[{"path":"/","method":"POST","body":null,"expected":1},{"path":"/","method":"POST","body":"x","expected":1}]`), "http://localhost")
	if err != nil || string(cases[0].body) != "null" || string(cases[1].body) != `"x"` {
		t.Fatal(cases, err)
	}
}

func TestCanonicalForm(t *testing.T) {
	equal := func(a, b string) bool {
		t.Helper()
		x, err := canonicalSHA256([]byte(a))
		if err != nil {
			t.Fatal(a, err)
		}
		y, err := canonicalSHA256([]byte(b))
		if err != nil {
			t.Fatal(b, err)
		}
		return x == y
	}
	for _, pair := range [][2]string{
		{`{"a":1,"b":55.0}`, `{"b":55,"a":1}`},
		{`{"x":{"b":[1,2],"a":null}}`, `{ "x" : { "a" : null , "b" : [ 1 , 2 ] } }`},
		{`{"n":-0}`, `{"n":0.0}`},
		{`{"n":1e2}`, `{"n":100}`},
		{`{"s":"\u00e9"}`, `{"s":"é"}`},
		{`{"n":0.1}`, `{"n":1e-1}`},
	} {
		if !equal(pair[0], pair[1]) {
			t.Fatal("unequal", pair)
		}
	}
	for _, pair := range [][2]string{
		{`{"a":1}`, `{"a":2}`},
		{`{"a":1}`, `{"a":"1"}`},
		{`{"a":1}`, `{"a":1,"b":null}`},
		{`[1,2]`, `[2,1]`},
		{`{"a":true}`, `{"a":1}`},
		{`{"n":9007199254740993}`, `{"n":9007199254740992}`},
	} {
		if equal(pair[0], pair[1]) {
			t.Fatal("equal", pair)
		}
	}
	got, err := canonicalJSON([]byte(`{"b":[1.5,2e0,-0.0,"x<",1e300,9007199254740993,9007199254740992.0,18014398509481984.0],"a":null,"c":{"z":false,"y":true}}`))
	want := `{"a":null,"b":[1.5,2,0,"x\u003c",1e+300,9007199254740993,9007199254740992,1.8014398509481984e+16],"c":{"y":true,"z":false}}`
	if err != nil || string(got) != want {
		t.Fatalf("%s %v", got, err)
	}
	for _, raw := range []string{`{"n":NaN}`, `{"n":1e400}`, `{"n":-1e400}`, `{"a":1,"a":1}`, `{"s":"\ud800"}`, `[1] [2]`} {
		if _, err := canonicalSHA256([]byte(raw)); err == nil {
			t.Fatal("accepted", raw)
		}
	}
}

// coldServer serves {"i":N} for /cold/N with X-Cache miss and the hot body {"n":1} with hit,
// recording the request order.
type coldServer struct {
	mutex sync.Mutex
	paths []string
	cache map[string][]string
}

func (server *coldServer) handler(w http.ResponseWriter, r *http.Request) {
	server.mutex.Lock()
	server.paths = append(server.paths, r.URL.Path)
	cache := server.cache[r.URL.Path]
	server.mutex.Unlock()
	w.Header().Set("Content-Type", "application/json")
	if index, ok := strings.CutPrefix(r.URL.Path, "/cold/"); ok {
		if cache == nil {
			cache = []string{"miss"}
		}
		for _, value := range cache {
			w.Header().Add("X-Cache", value)
		}
		io.WriteString(w, `{"i":`+index+`.0}`)
		return
	}
	w.Header().Set("X-Cache", "hit")
	io.WriteString(w, `{"n":1}`)
}

func coldFile(t *testing.T, count int, hashes bool) string {
	entries := make([]string, count)
	for i := range entries {
		hash := ""
		if hashes {
			hash = `,"canonical_sha256":"` + canonicalHex(`{"i":`+strconv.Itoa(i)+`}`) + `"`
		}
		entries[i] = `{"path":"/cold/` + strconv.Itoa(i) + `","params":{"k":"v"}` + hash + `}`
	}
	return caseFile(t, "["+strings.Join(entries, ",")+"]")
}

func TestColdMixThroughputOrder(t *testing.T) {
	recorder := &coldServer{}
	server := httptest.NewServer(http.HandlerFunc(recorder.handler))
	defer server.Close()
	args := throughputArgs(t, server.URL, 1)
	args.cases = caseFile(t, `[{"path":"/hot","expected":{"n":1}}]`)
	args.expectedCache = "hit-or-miss"
	args.warmup = 2
	args.duration = .1
	args.coldEvery = 10
	args.coldOffset = 7
	args.coldCases = coldFile(t, 5000, true)
	result, err := run(args)
	if err != nil {
		t.Fatal(err)
	}
	in := result["in_window"].(map[string]any)
	drain := result["drain"].(map[string]any)
	if in["errors"].(int64)+drain["errors"].(int64) != 0 {
		t.Fatal(result)
	}
	measured := recorder.paths[2:]
	cold := 0
	for index, path := range measured {
		if index%10 == 9 {
			if path != "/cold/"+strconv.Itoa(7+cold) {
				t.Fatalf("request %d: %s", index, path)
			}
			cold++
		} else if path != "/hot" {
			t.Fatalf("request %d: %s", index, path)
		}
	}
	if recorder.paths[0] != "/hot" || recorder.paths[1] != "/hot" || cold == 0 {
		t.Fatal(recorder.paths[:2], cold)
	}
	if result["cold_requests"].(int64) != int64(cold) || result["hot_requests"].(int64) != int64(len(measured)-cold+2) ||
		result["cold_checks"].(int64) != int64(cold) || result["cold_used"].(int64) != int64(6+cold) || result["cold_offset"].(int64) != 7 ||
		result["cold_consumed"].(int64) != int64(cold) || result["cold_exhausted"].(int64) != 0 {
		t.Fatal(result)
	}
	// Hot: one full check in warmup, everything else is byte-identical.
	if result["full_checks"].(int64) != 1 || result["memo_hits"].(int64) != int64(len(measured)-cold+1) {
		t.Fatal(result)
	}
	byKind := in["cache_by_kind"].(map[string]map[string]int64)
	if byKind["hot"]["hit"] != in["hot_requests"].(int64) || byKind["cold"]["miss"] != in["cold_requests"].(int64) || len(byKind["hot"]) != 1 || len(byKind["cold"]) != 1 {
		t.Fatal(byKind, in)
	}
	if _, err := json.Marshal(result); err != nil {
		t.Fatal(err)
	}
}

func TestColdPoolExhaustion(t *testing.T) {
	recorder := &coldServer{}
	server := httptest.NewServer(http.HandlerFunc(recorder.handler))
	defer server.Close()
	args := throughputArgs(t, server.URL, 1)
	args.cases = caseFile(t, `[{"path":"/hot","expected":{"n":1}}]`)
	args.expectedCache = "hit-or-miss"
	args.duration = .03
	args.coldEvery = 2
	args.coldOffset = 1
	args.coldCases = coldFile(t, 2, true)
	result, err := run(args)
	if err != nil {
		t.Fatal(err)
	}
	cold := 0
	for _, path := range recorder.paths {
		if strings.HasPrefix(path, "/cold/") {
			cold++
			if path != "/cold/1" {
				t.Fatal(path)
			}
		}
	}
	in := result["in_window"].(map[string]any)
	if cold != 1 || in["terminal_counts"].(map[string]int64)["client_errors"] == 0 || result["cold_exhausted"].(int64) == 0 || result["cold_used"].(int64) != 1 {
		t.Fatal(cold, result)
	}
	example := in["examples"].([]map[string]any)[0]
	if example["error"] != "cold pool exhausted" || example["category"] != "client_errors" {
		t.Fatal(example)
	}
	args.coldOffset = 2
	if _, err := run(args); err == nil {
		t.Fatal("offset outside pool")
	}
}

func TestColdCacheRuleAndHash(t *testing.T) {
	recorder := &coldServer{cache: map[string][]string{"/cold/1": {"hit"}, "/cold/2": {"miss", "miss"}, "/cold/3": {"bypass"}, "/cold/4": nil}}
	server := httptest.NewServer(http.HandlerFunc(recorder.handler))
	defer server.Close()
	cold, _, err := loadColdCases(coldFile(t, 5, true), server.URL, false)
	if err != nil {
		t.Fatal(err)
	}
	wrong := cold[0]
	wrong.canonical[0] ^= 1
	client := clientFor(options{connections: 1, phase: .2, deadline: .3})
	defer client.CloseIdleConnections()
	for _, c := range []struct {
		item            loadCase
		expectedCache   string
		category, cache string
	}{
		{cold[0], "hit-or-miss", "success", "miss"},
		{cold[1], "hit-or-miss", "bad_headers", "hit"},
		{cold[2], "hit-or-miss", "bad_headers", "miss"},
		{cold[3], "hit-or-miss", "bad_headers", "bypass"},
		{cold[3], "bypass", "success", "bypass"},
		{cold[0], "bypass", "bad_headers", "miss"},
		{cold[0], "", "success", "miss"},
		{wrong, "hit-or-miss", "wrong_body", "miss"},
	} {
		args := options{mode: "throughput", phase: .2, deadline: .3, maxBody: 1024, expectedCache: c.expectedCache}
		got := execute(client, c.item, args)
		if got.category != c.category || got.cache != c.cache || !got.cold || got.check != "cold" {
			t.Fatal(c.item.path, c.expectedCache, got)
		}
		if got.category == "bad_headers" && c.expectedCache == "hit-or-miss" && got.detail != "cold request needs exactly one X-Cache: miss" {
			t.Fatal(got.detail)
		}
	}
	for _, raw := range []string{`[{"path":"/x"}]`, `[{"path":"/x","canonical_sha256":"abc"}]`,
		`[{"path":"/x","canonical_sha256":"` + strings.ToUpper(canonicalHex("1")) + `"}]`, `[]`,
		`[{"path":"/x","canonical_sha256":"` + canonicalHex("1") + `","expected":1}]`} {
		if _, _, err := loadColdCases(caseFile(t, raw), server.URL, false); err == nil {
			t.Fatal("accepted", raw)
		}
	}
}

func TestColdMixOpenLoopWithPost(t *testing.T) {
	recorder := &coldServer{}
	var posts, badPosts atomic.Int64
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/hot" {
			raw, _ := io.ReadAll(r.Body)
			posts.Add(1)
			if r.Method != "POST" || string(raw) != `{"a":[1,2]}` || r.Header.Get("Content-Type") != "application/json" {
				badPosts.Add(1)
			}
		}
		recorder.handler(w, r)
	}))
	defer server.Close()
	result, err := run(options{address: server.URL, cases: caseFile(t, `[{"path":"/hot","method":"POST","body":{"a":[1,2]},"expected":{"n":1}}]`),
		rate: 2000, duration: .05, connections: 8, inflight: 256, phase: .5, deadline: 1, maxBody: 1024, procs: 2, p99: 200, errorBudget: 0, maxLag: 50,
		warmup: 1, coldEvery: 4, coldOffset: 3, coldCases: coldFile(t, 1000, true)})
	if err != nil {
		t.Fatal(err)
	}
	counts := result["counts"].(map[string]int64)
	if counts["drops"] != 0 || counts["success"] != counts["attempted"] || badPosts.Load() != 0 {
		t.Fatal(result)
	}
	seen := map[string]bool{}
	for _, path := range recorder.paths {
		if strings.HasPrefix(path, "/cold/") {
			if seen[path] {
				t.Fatal("repeated", path)
			}
			seen[path] = true
		}
	}
	cold := int64(len(seen))
	for i := int64(0); i < cold; i++ {
		if !seen["/cold/"+strconv.FormatInt(3+i, 10)] {
			t.Fatal("cold keys not consecutive from offset", seen)
		}
	}
	attempted := counts["attempted"]
	if cold != attempted/4 || result["cold_requests"].(int64) != cold || result["hot_requests"].(int64) != attempted-cold ||
		posts.Load() != attempted-cold+1 || result["cold_used"].(int64) != 2+cold {
		t.Fatal(cold, posts.Load(), result)
	}
	if result["full_checks"].(int64) != 0 || result["memo_hits"].(int64) != attempted-cold || result["cold_checks"].(int64) != cold {
		t.Fatal(result)
	}
	byKind := result["cache_by_kind_in_window"].(map[string]map[string]int64)
	if byKind["hot"]["hit"]+byKind["cold"]["miss"] != result["success_in_window"].(int64) {
		t.Fatal(byKind, result)
	}
	if _, err := json.Marshal(result); err != nil {
		t.Fatal(err)
	}
}

func TestRecordMode(t *testing.T) {
	var requests sync.Map
	var total atomic.Int64
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		total.Add(1)
		if _, loaded := requests.LoadOrStore(r.URL.RequestURI(), true); loaded {
			w.WriteHeader(500)
			return
		}
		switch r.URL.Path {
		case "/cold/404":
			w.WriteHeader(404)
			return
		case "/cold/text":
			w.Header().Set("Content-Type", "text/plain")
			io.WriteString(w, `{"i":1}`)
			return
		case "/cold/broken":
			w.Header().Set("Content-Type", "application/json")
			io.WriteString(w, `{"i":1,"i":1}`)
			return
		}
		index := strings.TrimPrefix(r.URL.Path, "/cold/")
		w.Header().Set("Content-Type", "application/json; charset=utf-8")
		io.WriteString(w, `{"z":[`+index+`.0, "\u00e9"], "a": `+index+`}`)
	}))
	defer server.Close()
	entries := make([]string, 50)
	for i := range entries {
		hash := ""
		if i%3 == 1 {
			hash = `,"canonical_sha256":""`
		} else if i%3 == 2 {
			hash = `,"canonical_sha256":"` + canonicalHex("stale") + `"`
		}
		entries[i] = fmt.Sprintf(`{"path":"/cold/%d","params":{"q":"%d"}%s}`, i, i, hash)
	}
	args := options{mode: "record", workers: 4, address: server.URL, coldCases: caseFile(t, "["+strings.Join(entries, ",")+"]"), phase: .5, deadline: 1, maxBody: 1024, procs: 2}
	files, err := runRecord(args)
	if err != nil {
		t.Fatal(err)
	}
	if len(files) != 50 || total.Load() != 50 {
		t.Fatal(len(files), total.Load())
	}
	for i, file := range files {
		want := canonicalHex(fmt.Sprintf(`{"a":%d,"z":[%d,"é"]}`, i, i))
		if file.Path != fmt.Sprintf("/cold/%d", i) || file.Params["q"] != strconv.Itoa(i) || file.CanonicalSHA256 != want {
			t.Fatal(i, file, want)
		}
	}
	encoded, err := json.Marshal(files[:1])
	if err != nil || string(encoded) != `[{"path":"/cold/0","params":{"q":"0"},"canonical_sha256":"`+canonicalHex(`{"a":0,"z":[0,"é"]}`)+`"}]` {
		t.Fatal(string(encoded), err)
	}
	recorded := caseFile(t, string(encoded))
	if _, _, err := loadColdCases(recorded, server.URL, false); err != nil {
		t.Fatal("recorded output not loadable", err)
	}
	for index, path := range []string{"/cold/404", "/cold/text", "/cold/broken"} {
		args.coldCases = caseFile(t, `[{"path":"/cold/`+strconv.Itoa(100+index)+`"},{"path":"`+path+`"}]`)
		if _, err := runRecord(args); err == nil || !strings.Contains(err.Error(), path) {
			t.Fatal(path, err)
		}
	}
}

func TestColdAndRecordCLI(t *testing.T) {
	good := [][]string{
		{"--cases", "x", "--rps", "10", "--cold-cases", "c", "--cold-every", "2"},
		{"--cases", "x", "--rps", "10", "--cold-cases", "c", "--cold-every", "10", "--cold-offset", "0"},
		{"--mode", "throughput", "--workers", "1", "--cases", "x", "--cold-cases", "c", "--cold-every", "5", "--cold-offset", "100", "--expected-cache", "hit-or-miss"},
		{"--mode", "record", "--cold-cases", "c", "--url", "http://x"},
		{"--mode", "record", "--cold-cases", "c", "--workers", "8", "--deadline", "10"},
	}
	for _, argv := range good {
		if _, err := parseOptions(argv); err != nil {
			t.Fatal(argv, err)
		}
	}
	if args, _ := parseOptions(good[3]); args.workers != 4 {
		t.Fatal("record default workers", args.workers)
	}
	for _, argv := range [][]string{
		{"--cases", "x", "--rps", "10", "--cold-cases", "c", "--cold-every", "1"},
		{"--cases", "x", "--rps", "10", "--cold-cases", "c", "--cold-every", "-2"},
		{"--cases", "x", "--rps", "10", "--cold-every", "4"},
		{"--cases", "x", "--rps", "10", "--cold-cases", "c"},
		{"--cases", "x", "--rps", "10", "--cold-offset", "3"},
		{"--cases", "x", "--rps", "10", "--cold-cases", "c", "--cold-every", "4", "--cold-offset", "-1"},
		{"--mode", "record"},
		{"--mode", "record", "--cold-cases", "c", "--cases", "x"},
		{"--mode", "record", "--cold-cases", "c", "--cold-every", "2"},
		{"--mode", "record", "--cold-cases", "c", "--cold-offset", "0"},
		{"--mode", "record", "--cold-cases", "c", "--workers", "0"},
		{"--mode", "record", "--cold-cases", "c", "--rps", "1"},
		{"--mode", "record", "--cold-cases", "c", "--expected-cache", "miss"},
		{"--mode", "record", "--cold-cases", "c", "--duration", "1"},
	} {
		if _, err := parseOptions(argv); err == nil {
			t.Fatal("accepted", argv)
		}
	}
}
