package main

import (
	"encoding/json"
	"math"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestQuery(t *testing.T) {
	for _, raw := range []string{"limit=0", "limit=1001", "unknown=1", "min_population=-1", "name_prefix=%00", "limit=1e2", "min_population=9223372036854775808"} {
		if _, err := parseQuery(raw); err == nil {
			t.Fatalf("accepted %s", raw)
		}
	}
	for _, raw := range []string{"", "limit=100", "limit=%2B100", "limit=100.0", "limit=1_00", "limit=100&limit=101"} {
		q, err := parseQuery(raw)
		if err != nil || q.Limit < 100 {
			t.Fatalf("rejected %s: %v %+v", raw, err, q)
		}
	}
	q, err := parseQuery("min_population=9223372036854775807&name_prefix=literal%25_&country_code=ZZ")
	if err != nil || *q.MinPopulation != math.MaxInt64 {
		t.Fatal(q, err)
	}
	sql, args := citySQL(q)
	if !strings.Contains(sql, "ESCAPE '/'") || !strings.Contains(sql, "JOIN countries") || args[2] != "literal/%/_" {
		t.Fatalf("SQL escaping: %s %#v", sql, args)
	}
}
func TestMapping(t *testing.T) {
	row := CityRow{ID: "local:city:1", Source: "local", Name: "Санкт-Петербург", CountryID: "local:country:1", Latitude: 1, Longitude: 2, Population: math.MaxInt64, FeatureCode: "PPL"}
	rows := []CityRow{row, row}
	rows[1].ID = "local:city:2"
	page, err := toPage(rows, 1)
	if err != nil || len(page.Items) != 1 || *page.NextAfterID != row.ID {
		t.Fatal(page, err)
	}
	body, err := json.Marshal(page)
	if err != nil || !strings.Contains(string(body), `"division_id":null`) || !strings.Contains(string(body), `9223372036854775807`) {
		t.Fatal(string(body), err)
	}
	empty, err := toPage(nil, 100)
	encoded, _ := json.Marshal(empty)
	if err != nil || string(encoded) != `{"items":[],"next_after_id":null}` {
		t.Fatal(string(encoded), err)
	}
	changes := []func(*CityRow){func(r *CityRow) { r.Name = "" }, func(r *CityRow) { r.Name = "\x00" }, func(r *CityRow) { r.Name = string([]byte{255}) }, func(r *CityRow) { r.Population = -1 }, func(r *CityRow) { r.Latitude = math.NaN() }, func(r *CityRow) { r.Longitude = 181 }, func(r *CityRow) { r.FeatureCode = "PPLQ" }, func(r *CityRow) { r.ID = "other:city:1" }}
	for _, change := range changes {
		bad := row
		change(&bad)
		if _, err := toPage([]CityRow{bad}, 1); err == nil {
			t.Fatalf("accepted %+v", bad)
		}
	}
}
func TestInvalidHTTPBeforeRepository(t *testing.T) {
	h := handler(Service{}) // Any repository access would panic, no DB is configured.
	for _, raw := range []string{"limit=0", "unknown=1", "min_population=-1"} {
		w := httptest.NewRecorder()
		h.ServeHTTP(w, httptest.NewRequest("GET", "/cities?"+raw, nil))
		if w.Code != 422 || w.Body.String() != `{"error":"invalid_fields"}` {
			t.Fatal(w.Code, w.Body.String())
		}
	}
}

func TestNoFieldPatterns(t *testing.T) {
    for _, raw := range []string{"country_code=zz", "country_code=long-code", "after_id=free%20text", "division_id=free%20text"} {
        if _, err := parseQuery(raw); err != nil { t.Fatalf("pattern-only input rejected: %s: %v", raw, err) }
    }
    v := City{ID: "local:city:free text!", Source: "local", Name: "City", CountryID: "free text!", Latitude: 1, Longitude: 2, Population: 0, FeatureCode: "PPL"}
    if err := validateCity(v); err != nil { t.Fatal(err) }
    v.CountryID = ""
    if err := validateCity(v); err == nil { t.Fatal("empty ID accepted") }
    v.CountryID = "bad\x00id"
    if err := validateCity(v); err == nil { t.Fatal("NUL ID accepted") }
}
