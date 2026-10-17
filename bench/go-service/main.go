package main

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"math"
	"net/http"
	"net/url"
	"os"
	"regexp"
	"strconv"
	"strings"
	"time"
	"unicode"
	"unicode/utf8"

	_ "github.com/jackc/pgx/v5/stdlib"
)

var integer = regexp.MustCompile(`^-?(0|[1-9][0-9]*)$`)
var invalid = errors.New("invalid_fields")

// CityRow contains database values. City is the HTTP contract.
type CityRow struct {
	ID, Source, Name, CountryID string
	DivisionID                  *string
	Latitude, Longitude         float64
	Population                  int64
	FeatureCode                 string
}
type City struct {
	ID          string  `json:"id"`
	Source      string  `json:"source"`
	Name        string  `json:"name"`
	CountryID   string  `json:"country_id"`
	DivisionID  *string `json:"division_id"`
	Latitude    float64 `json:"latitude"`
	Longitude   float64 `json:"longitude"`
	Population  int64   `json:"population"`
	FeatureCode string  `json:"feature_code"`
}
type Page struct {
	Items       []City  `json:"items"`
	NextAfterID *string `json:"next_after_id"`
}
type Query struct {
	Limit                                                int
	AfterID, NamePrefix, CountryCode, Source, DivisionID *string
	MinPopulation                                        *int64
}

func validText(s string, min, max int) bool {
	n := utf8.RuneCountInString(s)
	return utf8.ValidString(s) && !strings.ContainsRune(s, 0) && n >= min && n <= max
}
func validID(s string) bool { return validText(s, 1, 160) }

// Match the pinned adapter's HTTP integer normalization, including decimal .0.
func httpInt(s string) (int64, error) {
	if integer.MatchString(s) {
		return strconv.ParseInt(s, 10, 64)
	}
	number := strings.TrimFunc(s, unicode.IsSpace)
	if strings.HasPrefix(number, "+") {
		number = number[1:]
		if strings.HasPrefix(number, "-") {
			return 0, invalid
		}
	}
	negative := strings.HasPrefix(number, "-")
	if negative {
		number = number[1:]
		if strings.HasPrefix(number, "-") || strings.HasPrefix(number, "+") {
			return 0, invalid
		}
	}
	if len(number) == 0 || !strings.ContainsRune("0123456789-", rune(number[0])) {
		return 0, invalid
	}
	if number[0] == '0' {
		done := false
		for i := 1; i < len(number); i++ {
			ch := number[i]
			if ch == '0' || ch == '_' {
				continue
			}
			if strings.ContainsRune("123456789-", rune(ch)) {
				number = number[i:]
				done = true
				break
			}
			if ch == '.' {
				number = number[i-1:]
				done = true
				break
			}
			return 0, invalid
		}
		if !done {
			number = number[len(number)-1:]
		}
	}
	if whole, decimal, ok := strings.Cut(number, "."); ok && len(decimal) > 0 && strings.Trim(decimal, "0") == "" {
		number = whole
	}
	if strings.Contains(number, "_") && !strings.HasPrefix(number, "_") && !strings.HasSuffix(number, "_") && !strings.Contains(number, "__") {
		number = strings.ReplaceAll(number, "_", "")
	}
	if negative {
		number = "-" + number
	}
	if !integer.MatchString(number) {
		return 0, invalid
	}
	return strconv.ParseInt(number, 10, 64)
}
func parseQuery(raw string) (Query, error) {
	q := Query{Limit: 100}
	values, err := url.ParseQuery(raw)
	if err != nil {
		return q, invalid
	}
	for key, list := range values {
		value := list[len(list)-1] // FastAPI scalar query parameters use the last occurrence.
		if !utf8.ValidString(value) {
			return q, invalid
		}
		switch key {
		case "limit":
			n, e := httpInt(value)
			if e != nil || n < 1 || n > 1000 {
				return q, invalid
			}
			q.Limit = int(n)
		case "min_population":
			n, e := httpInt(value)
			if e != nil || n < 0 {
				return q, invalid
			}
			q.MinPopulation = &n
		case "after_id":
			if !validID(value) {
				return q, invalid
			}
			q.AfterID = &value
		case "division_id":
			if !validID(value) {
				return q, invalid
			}
			q.DivisionID = &value
		case "name_prefix":
			if !validText(value, 1, 200) {
				return q, invalid
			}
			q.NamePrefix = &value
		case "country_code":
			q.CountryCode = &value
		case "source":
			if value != "local" && value != "geonames" && value != "ourairports" {
				return q, invalid
			}
			q.Source = &value
		default:
			return q, invalid
		}
	}
	return q, nil
}

func citySQL(q Query) (string, []any) {
	statement := "SELECT cities.id, cities.source, cities.name, cities.country_id, cities.division_id, cities.latitude, cities.longitude, cities.population, cities.feature_code FROM cities"
	if q.CountryCode != nil {
		statement += " JOIN countries ON cities.country_id = countries.id"
	}
	args := []any{}
	clauses := []string{}
	add := func(expression string, value any) {
		args = append(args, value)
		clauses = append(clauses, fmt.Sprintf(expression, len(args)))
	}
	if q.CountryCode != nil {
		add("countries.country_code = $%d", *q.CountryCode)
	}
	if q.Source != nil {
		add("cities.source = $%d", *q.Source)
	}
	if q.DivisionID != nil {
		add("cities.division_id = $%d", *q.DivisionID)
	}
	if q.MinPopulation != nil {
		add("cities.population >= $%d", *q.MinPopulation)
	}
	if q.NamePrefix != nil {
		escaped := strings.NewReplacer("/", "//", "%", "/%", "_", "/_").Replace(*q.NamePrefix)
		add("cities.name LIKE ($%d || '%%') ESCAPE '/'", escaped)
	}
	if q.AfterID != nil {
		add("cities.id > $%d", *q.AfterID)
	}
	if len(clauses) > 0 {
		statement += " WHERE " + strings.Join(clauses, " AND ")
	}
	args = append(args, q.Limit+1)
	statement += fmt.Sprintf(" ORDER BY cities.id LIMIT $%d", len(args))
	return statement, args
}

type Repository struct{ DB *sql.DB }

func (r Repository) read(ctx context.Context, q Query) ([]CityRow, error) {
	tx, err := r.DB.BeginTx(ctx, nil)
	if err != nil {
		return nil, err
	}
	defer tx.Rollback()
	if _, err = tx.ExecContext(ctx, "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"); err != nil {
		return nil, err
	}
	// Retain the generation lookup performed by the Python control even in bypass.
	var version int64
	if err = tx.QueryRowContext(ctx, "SELECT dataset_version.value FROM dataset_version WHERE dataset_version.id = 1").Scan(&version); err != nil {
		return nil, err
	}
	statement, args := citySQL(q)
	rows, err := tx.QueryContext(ctx, statement, args...)
	if err != nil {
		return nil, err
	}
	result := make([]CityRow, 0, q.Limit+1)
	for rows.Next() {
		var v CityRow
		if err = rows.Scan(&v.ID, &v.Source, &v.Name, &v.CountryID, &v.DivisionID, &v.Latitude, &v.Longitude, &v.Population, &v.FeatureCode); err != nil {
			rows.Close()
			return nil, err
		}
		result = append(result, v)
	}
	err = rows.Err()
	rows.Close()
	if err != nil {
		return nil, err
	}
	if err = tx.Commit(); err != nil {
		return nil, err
	}
	return result, nil
}

type Service struct {
	Repository Repository
	cache      *viewCache
}

func (s Service) cities(ctx context.Context, q Query) ([]CityRow, error) {
	return s.Repository.read(ctx, q)
}
func validateCity(v City) error {
	if !validID(v.ID) || !validID(v.CountryID) || (v.DivisionID != nil && !validID(*v.DivisionID)) || !validText(v.Name, 1, 200) {
		return invalid
	}
	if v.Source != "local" && v.Source != "geonames" {
		return invalid
	}
	if !strings.HasPrefix(v.ID, v.Source+":city:") {
		return invalid
	}
	if math.IsNaN(v.Latitude) || math.IsInf(v.Latitude, 0) || v.Latitude < -90 || v.Latitude > 90 || math.IsNaN(v.Longitude) || math.IsInf(v.Longitude, 0) || v.Longitude < -180 || v.Longitude > 180 || v.Population < 0 {
		return invalid
	}
	switch v.FeatureCode {
	case "PPL", "PPLA", "PPLA2", "PPLA3", "PPLA4", "PPLA5", "PPLC":
	default:
		return invalid
	}
	return nil
}
func toPage(rows []CityRow, limit int) (Page, error) {
	result := Page{Items: make([]City, 0, min(len(rows), limit))}
	if len(rows) > limit {
		cursor := rows[limit-1].ID
		result.NextAfterID = &cursor
		rows = rows[:limit]
	}
	for _, row := range rows {
		v := City{row.ID, row.Source, row.Name, row.CountryID, row.DivisionID, row.Latitude, row.Longitude, row.Population, row.FeatureCode}
		if err := validateCity(v); err != nil {
			return Page{}, err
		}
		result.Items = append(result.Items, v)
	}
	return result, nil
}
func handler(service Service) http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("/cities", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != "GET" {
			w.Header().Set("Allow", "GET")
			http.Error(w, "Method Not Allowed", 405)
			return
		}
		query, err := parseQuery(r.URL.RawQuery)
		if err != nil {
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(422)
			w.Write([]byte(`{"error":"invalid_fields"}`))
			return
		}
		if service.cache != nil {
			serveCached(w, r, service, query)
			return
		}
		rows, err := service.cities(r.Context(), query)
		if err != nil {
			log.Printf("repository: %v", err)
			http.Error(w, "Internal Server Error", 500)
			return
		}
		page, err := toPage(rows, query.Limit)
		if err != nil {
			http.Error(w, "Internal Server Error", 500)
			return
		}
		body, err := json.Marshal(page)
		if err != nil {
			http.Error(w, "Internal Server Error", 500)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		w.Header().Set("X-Cache", "bypass")
		w.Write(body)
	})
	return mux
}
func main() {
	db, err := sql.Open("pgx", os.Getenv("DATABASE_URL"))
	if err != nil {
		log.Fatal(err)
	}
	defer db.Close()
	db.SetMaxOpenConns(10)
	db.SetMaxIdleConns(10)
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if err = db.PingContext(ctx); err != nil {
		log.Fatal(err)
	}
	addr := os.Getenv("LISTEN_ADDR")
	if addr == "" {
		addr = ":8000"
	}
	service := Service{Repository: Repository{db}}
	switch os.Getenv("VIEW_CACHE") {
	case "enabled":
		service.cache = newViewCache(os.Getenv("REDIS_ADDR"), 60, 10)
	case "", "disabled":
	default:
		log.Fatal("VIEW_CACHE must be enabled or disabled")
	}
	server := http.Server{Addr: addr, Handler: handler(service), ReadHeaderTimeout: 5 * time.Second}
	log.Fatal(server.ListenAndServe())
}

func serveCached(w http.ResponseWriter, r *http.Request, service Service, query Query) {
	key := cacheKey(query)
	state := "hit"
	raw, ok, err := service.cache.get(key)
	if err != nil {
		cacheUnavailable(w, err)
		return
	}
	if !ok {
		state = "miss"
		rows, err := service.cities(r.Context(), query)
		if err != nil {
			log.Printf("repository: %v", err)
			http.Error(w, "Internal Server Error", 500)
			return
		}
		page, err := toPage(rows, query.Limit)
		if err != nil {
			http.Error(w, "Internal Server Error", 500)
			return
		}
		if raw, err = json.Marshal(page); err != nil {
			http.Error(w, "Internal Server Error", 500)
			return
		}
		if err = service.cache.set(key, raw); err != nil {
			cacheUnavailable(w, err)
			return
		}
	}
	body, err := checkedPage(raw)
	if err != nil {
		log.Printf("cached page: %v", err)
		http.Error(w, "Internal Server Error", 500)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("X-Cache", state)
	w.Write(body)
}

func cacheUnavailable(w http.ResponseWriter, err error) {
	log.Printf("cache: %v", err)
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(503)
	w.Write([]byte(`{"error":"cache_unavailable"}`))
}
