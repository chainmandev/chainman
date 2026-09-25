package main

import (
	"context"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	syncatomic "sync/atomic"
	"testing"
	"time"
)

func TestHTTPProbePredicatesAndPrivacy(t *testing.T) {
	var redirected syncatomic.Int32
	target := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { redirected.Add(1) }))
	defer target.Close()
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/redirect":
			http.Redirect(w, r, target.URL, 302)
		case "/wrong":
			w.Write([]byte("wrong secret response"))
		case "/large":
			w.Write([]byte(strings.Repeat(" ", maxProbeBody+1) + "OK"))
		case "/slow":
			<-r.Context().Done()
		case "/body-slow":
			w.WriteHeader(200)
			w.(http.Flusher).Flush()
			<-r.Context().Done()
		default:
			if r.Header.Get("Authorization") != "Bearer private-fixture" {
				w.WriteHeader(401)
				return
			}
			w.Write([]byte(" OK\n"))
		}
	}))
	defer server.Close()
	port := server.Listener.Addr().(*net.TCPAddr).Port
	expected := "OK"
	for _, tc := range []struct {
		name, path string
		headers    map[string]string
		trim, ok   bool
	}{
		{"authorized", "/", map[string]string{"Authorization": "Bearer private-fixture"}, true, true},
		{"exact-body", "/", map[string]string{"Authorization": "Bearer private-fixture"}, false, false},
		{"unauthorized", "/", nil, true, false},
		{"wrong-body", "/wrong", nil, true, false},
		{"oversize", "/large", nil, true, false},
		{"redirect", "/redirect", map[string]string{"Authorization": "Bearer private-fixture"}, true, false},
		{"deadline", "/slow", nil, true, false},
		{"body-deadline", "/body-slow", nil, true, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			ctx, cancel := context.WithTimeout(context.Background(), 100*time.Millisecond)
			defer cancel()
			err := checkHTTP(ctx, &HTTPProbe{Port: port, Path: tc.path, StatusCode: 200, Body: &expected, TrimBody: tc.trim, Headers: tc.headers})
			if (err == nil) != tc.ok {
				t.Fatalf("unexpected outcome: %v", err)
			}
			if err != nil && (strings.Contains(err.Error(), "private-fixture") || strings.Contains(err.Error(), "secret response")) {
				t.Fatal("probe leaked private data")
			}
		})
	}
	if redirected.Load() != 0 {
		t.Fatal("followed redirect")
	}
	t.Setenv("HTTP_PROXY", target.URL)
	t.Setenv("ALL_PROXY", target.URL)
	if err := checkHTTP(context.Background(), &HTTPProbe{Port: port, Path: "/", StatusCode: 200, Body: &expected, TrimBody: true, Headers: map[string]string{"Authorization": "Bearer private-fixture"}}); err != nil {
		t.Fatal(err)
	}
	if redirected.Load() != 0 {
		t.Fatal("inherited proxy")
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if checkHTTP(ctx, &HTTPProbe{Port: port, Path: "/slow", StatusCode: 200}) == nil {
		t.Fatal("ignored cancellation")
	}
}
func TestHTTPProbeHeaderBounds(t *testing.T) {
	for _, headers := range []map[string]string{
		{"Authorization": "secret\r\ninjected: yes"}, {"Host": "elsewhere"}, {"Connection": "upgrade"},
		{"Authorization": "one", "authorization": "two"}, {"bad name": "x"}, {"X-Test": strings.Repeat("x", 8193)},
	} {
		if validateHTTPProbe(&HTTPProbe{Port: 80, Path: "/", StatusCode: 200, Headers: headers}) == nil {
			t.Fatal("invalid headers accepted")
		}
	}
}
