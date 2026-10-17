package main

import (
	"bufio"
	"fmt"
	"io"
	"net"
	"net/http/httptest"
	"strconv"
	"strings"
	"sync"
	"testing"
)

// fakeRedis answers GET and SET with a RESP2 subset backed by a map.
type fakeRedis struct {
	mu   sync.Mutex
	data map[string]string
	fail bool
	ln   net.Listener
}

func startFakeRedis(t *testing.T) *fakeRedis {
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	f := &fakeRedis{data: map[string]string{}, ln: ln}
	go func() {
		for {
			c, err := ln.Accept()
			if err != nil {
				return
			}
			go f.serve(c)
		}
	}()
	t.Cleanup(func() { ln.Close() })
	return f
}

func (f *fakeRedis) serve(c net.Conn) {
	defer c.Close()
	r := bufio.NewReader(c)
	for {
		line, err := r.ReadString('\n')
		if err != nil {
			return
		}
		n, _ := strconv.Atoi(strings.TrimSpace(line[1:]))
		args := make([]string, n)
		for i := range args {
			h, _ := r.ReadString('\n')
			size, _ := strconv.Atoi(strings.TrimSpace(h[1:]))
			buf := make([]byte, size+2)
			if _, err := io.ReadFull(r, buf); err != nil {
				return
			}
			args[i] = string(buf[:size])
		}
		f.mu.Lock()
		switch {
		case f.fail:
			fmt.Fprint(c, "-ERR injected\r\n")
		case args[0] == "GET":
			if v, ok := f.data[args[1]]; ok {
				fmt.Fprintf(c, "$%d\r\n%s\r\n", len(v), v)
			} else {
				fmt.Fprint(c, "$-1\r\n")
			}
		case args[0] == "SET" && len(args) == 5 && args[3] == "EX" && args[4] == "60":
			f.data[args[1]] = args[2]
			fmt.Fprint(c, "+OK\r\n")
		default:
			fmt.Fprint(c, "-ERR unexpected command\r\n")
		}
		f.mu.Unlock()
	}
}

const validCity = `{"id":"geonames:city:1","source":"geonames","name":"Moscow","country_id":"geonames:country:RU","division_id":null,"latitude":55.75,"longitude":37.62,"population":13000000,"feature_code":"PPLC"}`

func cachedService(f *fakeRedis) Service {
	// The zero Repository panics on any database access, so a hit must not use it.
	return Service{cache: newViewCache(f.ln.Addr().String(), 60, 2)}
}

func TestCacheHitSkipsDatabaseAndValidates(t *testing.T) {
	f := startFakeRedis(t)
	q, err := parseQuery("limit=1")
	if err != nil {
		t.Fatal(err)
	}
	f.data[cacheKey(q)] = `{"items":[` + validCity + `],"next_after_id":"geonames:city:1"}`
	rec := httptest.NewRecorder()
	handler(cachedService(f)).ServeHTTP(rec, httptest.NewRequest("GET", "/cities?limit=1", nil))
	if rec.Code != 200 || rec.Header().Get("X-Cache") != "hit" || !strings.Contains(rec.Body.String(), `"name":"Moscow"`) {
		t.Fatalf("unexpected response %d %q %s", rec.Code, rec.Header().Get("X-Cache"), rec.Body)
	}
}

func TestCorruptedCacheEntryIsRejected(t *testing.T) {
	q, _ := parseQuery("limit=1")
	for name, entry := range map[string]string{
		"negative population": strings.Replace(validCity, `13000000`, `-1`, 1),
		"wrong namespace":     strings.Replace(validCity, `geonames:city:1`, `local:city:1`, 1),
		"unknown field":       strings.Replace(validCity, `{"id"`, `{"extra":1,"id"`, 1),
		"string population":   strings.Replace(validCity, `13000000`, `"13000000"`, 1),
	} {
		f := startFakeRedis(t)
		f.data[cacheKey(q)] = `{"items":[` + entry + `],"next_after_id":null}`
		rec := httptest.NewRecorder()
		handler(cachedService(f)).ServeHTTP(rec, httptest.NewRequest("GET", "/cities?limit=1", nil))
		if rec.Code != 500 {
			t.Fatalf("%s: expected 500, got %d %s", name, rec.Code, rec.Body)
		}
	}
}

func TestCacheErrorIs503(t *testing.T) {
	f := startFakeRedis(t)
	f.fail = true
	rec := httptest.NewRecorder()
	handler(cachedService(f)).ServeHTTP(rec, httptest.NewRequest("GET", "/cities?limit=1", nil))
	if rec.Code != 503 || rec.Body.String() != `{"error":"cache_unavailable"}` {
		t.Fatalf("unexpected response %d %s", rec.Code, rec.Body)
	}
}

func TestCacheKeyCoversQuery(t *testing.T) {
	a, _ := parseQuery("limit=3&country_code=RU")
	b, _ := parseQuery("limit=3&country_code=FR")
	c, _ := parseQuery("limit=4&country_code=RU")
	if cacheKey(a) == cacheKey(b) || cacheKey(a) == cacheKey(c) {
		t.Fatal("cache key ignores query parameters")
	}
}

func TestCheckedPageMatchesStrictContract(t *testing.T) {
	page := func(item string) string { return `{"items":[` + item + `],"next_after_id":null}` }
	rejected := map[string]string{
		"missing population":  page(strings.Replace(validCity, `,"population":13000000`, ``, 1)),
		"null population":     page(strings.Replace(validCity, `13000000`, `null`, 1)),
		"null latitude":       page(strings.Replace(validCity, `55.75`, `null`, 1)),
		"float population":    page(strings.Replace(validCity, `13000000`, `1.0`, 1)),
		"key case":            page(strings.Replace(validCity, `"name"`, `"NAME"`, 1)),
		"null name":           page(strings.Replace(validCity, `"Moscow"`, `null`, 1)),
		"lone high surrogate": page(strings.Replace(validCity, `Moscow`, `Mo\ud800scow`, 1)),
		"lone low surrogate":  page(strings.Replace(validCity, `Moscow`, `Mo\udc00scow`, 1)),
		"invalid utf8":        page(strings.Replace(validCity, `Moscow`, "Mo\xffscow", 1)),
		"missing next cursor": `{"items":[` + validCity + `]}`,
		"extra top-level key": `{"items":[` + validCity + `],"next_after_id":null,"x":1}`,
		"null items":          `{"items":null,"next_after_id":null}`,
		"trailing data":       page(validCity) + ` {}`,
		"trailing garbage":    page(validCity) + `x`,
		"null item":           page(`null`),
	}
	for name, raw := range rejected {
		if _, err := checkedPage([]byte(raw)); err == nil {
			t.Errorf("%s: accepted", name)
		}
	}
	// Go accepts only the canonical encoding it writes itself, which is stricter
	// than msgspec for these valid but differently encoded inputs.
	for name, raw := range map[string]string{
		"escaped surrogate pair": page(strings.Replace(validCity, `Moscow`, `Mo\ud83d\ude00scow`, 1)),
		"duplicate key":          page(strings.Replace(validCity, `"population":13000000`, `"population":-1,"population":13000000`, 1)),
		"trailing space":         page(validCity) + " \n",
		"reordered keys":         `{"next_after_id":null,"items":[` + validCity + `]}`,
	} {
		if _, err := checkedPage([]byte(raw)); err == nil {
			t.Errorf("non-canonical %s: accepted", name)
		}
	}
	accepted := map[string]string{
		"valid":               page(validCity),
		"integer latitude":    page(strings.Replace(validCity, `55.75`, `55`, 1)),
		"empty items":         `{"items":[],"next_after_id":null}`,
		"division and cursor": `{"items":[` + strings.Replace(validCity, `"division_id":null`, `"division_id":"geonames:division:1"`, 1) + `],"next_after_id":"geonames:city:1"}`,
		"utf8 name":           page(strings.Replace(validCity, `Moscow`, `Москва`, 1)),
	}
	for name, raw := range accepted {
		if _, err := checkedPage([]byte(raw)); err != nil {
			t.Errorf("%s: rejected: %v", name, err)
		}
	}
}
