package main

import (
	"bufio"
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"strconv"
	"time"
)

// viewCache stores the encoded cities page in Redis with a fixed TTL, like the
// Cashews view cache of the Python variants. Only GET and SET EX are needed,
// so a minimal RESP2 client keeps the module dependencies unchanged.
type viewCache struct {
	addr  string
	ttl   int
	conns chan *redisConn
}

type redisConn struct {
	c net.Conn
	r *bufio.Reader
}

var errCache = errors.New("cache_unavailable")

func newViewCache(addr string, ttl, size int) *viewCache {
	return &viewCache{addr: addr, ttl: ttl, conns: make(chan *redisConn, size)}
}

func (v *viewCache) conn() (*redisConn, error) {
	select {
	case c := <-v.conns:
		return c, nil
	default:
	}
	c, err := net.DialTimeout("tcp", v.addr, 2*time.Second)
	if err != nil {
		return nil, err
	}
	return &redisConn{c: c, r: bufio.NewReader(c)}, nil
}

func (v *viewCache) release(c *redisConn, err error) {
	if err != nil {
		c.c.Close()
		return
	}
	select {
	case v.conns <- c:
	default:
		c.c.Close()
	}
}

// do sends one command and returns a bulk or simple string reply; nil reply
// is reported with ok=false.
func (v *viewCache) do(args ...string) (reply []byte, ok bool, err error) {
	c, err := v.conn()
	if err != nil {
		return nil, false, err
	}
	defer func() { v.release(c, err) }()
	if err = c.c.SetDeadline(time.Now().Add(2 * time.Second)); err != nil {
		return nil, false, err
	}
	var b bytes.Buffer
	fmt.Fprintf(&b, "*%d\r\n", len(args))
	for _, a := range args {
		fmt.Fprintf(&b, "$%d\r\n%s\r\n", len(a), a)
	}
	if _, err = c.c.Write(b.Bytes()); err != nil {
		return nil, false, err
	}
	line, err := c.r.ReadBytes('\n')
	if err != nil {
		return nil, false, err
	}
	if len(line) < 3 || line[len(line)-2] != '\r' {
		return nil, false, errors.New("redis: malformed reply")
	}
	body := line[1 : len(line)-2]
	switch line[0] {
	case '+':
		return body, true, nil
	case '-':
		return nil, false, fmt.Errorf("redis: %s", body)
	case '$':
		n, perr := strconv.Atoi(string(body))
		if perr != nil {
			return nil, false, perr
		}
		if n < 0 {
			return nil, false, nil
		}
		buf := make([]byte, n+2)
		if _, err = io.ReadFull(c.r, buf); err != nil {
			return nil, false, err
		}
		return buf[:n], true, nil
	}
	return nil, false, errors.New("redis: unexpected reply type")
}

func (v *viewCache) get(key string) ([]byte, bool, error) { return v.do("GET", key) }
func (v *viewCache) set(key string, value []byte) error {
	_, _, err := v.do("SET", key, string(value), "EX", strconv.Itoa(v.ttl))
	return err
}

// cacheKey includes every validated query parameter.
func cacheKey(q Query) string {
	raw, _ := json.Marshal(struct {
		Limit         int     `json:"limit"`
		AfterID       *string `json:"after_id"`
		NamePrefix    *string `json:"name_prefix"`
		CountryCode   *string `json:"country_code"`
		Source        *string `json:"source"`
		DivisionID    *string `json:"division_id"`
		MinPopulation *int64  `json:"min_population"`
	}{q.Limit, q.AfterID, q.NamePrefix, q.CountryCode, q.Source, q.DivisionID, q.MinPopulation})
	sum := sha256.Sum256(raw)
	return "view:cities:" + hex.EncodeToString(sum[:])
}

// cachedCity and cachedPage detect missing fields and nulls: pointers stay nil
// when a required field is absent or null; nullable fields keep the raw value.
type cachedCity struct {
	ID          *string         `json:"id"`
	Source      *string         `json:"source"`
	Name        *string         `json:"name"`
	CountryID   *string         `json:"country_id"`
	DivisionID  json.RawMessage `json:"division_id"`
	Latitude    *float64        `json:"latitude"`
	Longitude   *float64        `json:"longitude"`
	Population  *int64          `json:"population"`
	FeatureCode *string         `json:"feature_code"`
}
type cachedPage struct {
	Items       *[]*cachedCity  `json:"items"`
	NextAfterID json.RawMessage `json:"next_after_id"`
}

// checkedPage validates cached data before every reply, at least as strictly
// as msgspec.convert(strict=True) in the Python variants: every field present,
// null only where nullable, no unknown fields, value checks of validateCity.
// The re-encoded page must also equal the cached bytes, so only the canonical
// encoding this service writes is accepted; this rejects other key spellings,
// duplicate keys, invalid UTF-8, lone surrogates and trailing data.
func checkedPage(raw []byte) ([]byte, error) {
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.DisallowUnknownFields()
	var in cachedPage
	if err := dec.Decode(&in); err != nil {
		return nil, err
	}
	if in.Items == nil || len(in.NextAfterID) == 0 {
		return nil, invalid
	}
	page := Page{Items: make([]City, 0, len(*in.Items))}
	for _, c := range *in.Items {
		if c == nil || c.ID == nil || c.Source == nil || c.Name == nil || c.CountryID == nil || len(c.DivisionID) == 0 || c.Latitude == nil || c.Longitude == nil || c.Population == nil || c.FeatureCode == nil {
			return nil, invalid
		}
		v := City{ID: *c.ID, Source: *c.Source, Name: *c.Name, CountryID: *c.CountryID, Latitude: *c.Latitude, Longitude: *c.Longitude, Population: *c.Population, FeatureCode: *c.FeatureCode}
		if string(c.DivisionID) != "null" {
			var d string
			if err := json.Unmarshal(c.DivisionID, &d); err != nil {
				return nil, invalid
			}
			v.DivisionID = &d
		}
		if err := validateCity(v); err != nil {
			return nil, err
		}
		page.Items = append(page.Items, v)
	}
	if string(in.NextAfterID) != "null" {
		var cursor string
		if err := json.Unmarshal(in.NextAfterID, &cursor); err != nil || !validID(cursor) {
			return nil, invalid
		}
		page.NextAfterID = &cursor
	}
	out, err := json.Marshal(page)
	if err != nil {
		return nil, err
	}
	if !bytes.Equal(out, raw) {
		return nil, invalid
	}
	return out, nil
}
