package main

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"syscall"
	"testing"
	"time"
)

// Realistic body: the single case of the benchmark cases file, expected value re-encoded.
func benchCase(b *testing.B) (loadCase, []byte) {
	b.Helper()
	path := "../bench/cases.json"
	raw, err := os.ReadFile(path)
	if err != nil {
		b.Skip("bench cases unavailable:", err)
	}
	var files []fileCase
	if err := json.Unmarshal(raw, &files); err != nil || len(files) != 1 {
		b.Fatal(err, len(files))
	}
	decoded, err := decodeJSON(files[0].Expected)
	if err != nil {
		b.Fatal(err)
	}
	body, err := json.Marshal(decoded)
	if err != nil {
		b.Fatal(err)
	}
	cases, err := loadCases(path, "http://localhost")
	if err != nil {
		b.Fatal(err)
	}
	return cases[0], body
}

func processCPU() time.Duration {
	var usage syscall.Rusage
	syscall.Getrusage(syscall.RUSAGE_SELF, &usage)
	return time.Duration(usage.Utime.Nano() + usage.Stime.Nano())
}

func BenchmarkVerifyBody(b *testing.B) {
	item, body := benchCase(b)
	for _, memo := range []bool{false, true} {
		name := "full"
		if memo {
			name = "memo"
		}
		b.Run(name, func(b *testing.B) {
			item := item
			item.verified = nil
			if memo {
				item.verified = &verifiedBodies{}
				if category, check, _, _ := item.verify(append([]byte(nil), body...), false); category != "success" || check != "full" {
					b.Fatal(category, check)
				}
			}
			incoming := append([]byte(nil), body...) // separate allocation, as from io.ReadAll
			want := "full"
			if memo {
				want = "memo"
			}
			b.SetBytes(int64(len(body)))
			b.ReportAllocs()
			b.ResetTimer()
			for b.Loop() {
				if category, check, _, _ := item.verify(incoming, false); category != "success" || check != want {
					b.Fatal(category, check)
				}
			}
		})
	}
}

// End to end through execute against an in-process server; cpu-ns/op is whole-process CPU and
// therefore includes the httptest server writing the fixed body.
func BenchmarkExecuteBody(b *testing.B) {
	item, body := benchCase(b)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.Header().Set("X-Cache", "hit")
		w.Write(body)
	}))
	defer server.Close()
	item.address = server.URL
	args := options{mode: "throughput", phase: 2, deadline: 5, maxBody: 8 << 20, expectedCache: "hit"}
	client := clientFor(options{connections: 1, phase: 2, deadline: 5})
	defer client.CloseIdleConnections()
	for _, memo := range []bool{false, true} {
		name := "full"
		if memo {
			name = "memo"
		}
		b.Run(name, func(b *testing.B) {
			item := item
			item.verified = nil
			if memo {
				item.verified = &verifiedBodies{}
			}
			execute(client, item, args)
			b.SetBytes(int64(len(body)))
			b.ResetTimer()
			cpu := processCPU()
			for b.Loop() {
				if result := execute(client, item, args); result.category != "success" {
					b.Fatal(result)
				}
			}
			b.ReportMetric(float64(processCPU()-cpu)/float64(b.N), "cpu-ns/op")
		})
	}
}
