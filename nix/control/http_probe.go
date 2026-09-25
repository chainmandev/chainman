package main

import (
	"context"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"path/filepath"
	"strings"
	"time"
	"unicode/utf8"
)

const maxProbeBody = 64 * 1024

func validateHTTPProbe(h *HTTPProbe) error {
	u, err := url.ParseRequestURI(h.Path)
	if h.Port < 1 || h.Port > 65535 || h.StatusCode < 200 || h.StatusCode > 299 || err != nil || u.IsAbs() || !strings.HasPrefix(h.Path, "/") || strings.HasPrefix(h.Path, "//") || strings.ContainsAny(h.Path, "# \t\r\n") {
		return fmt.Errorf("invalid HTTP readiness declaration")
	}
	if (h.TrimBody && h.Body == nil) || (h.Body != nil && (!utf8.ValidString(*h.Body) || len(*h.Body) > 4096)) {
		return fmt.Errorf("invalid HTTP readiness body predicate")
	}
	size := 0
	names := map[string]bool{}
	for key, value := range h.Headers {
		canonical := http.CanonicalHeaderKey(key)
		if !validHTTPHeader(key) || names[canonical] || strings.ContainsAny(value, "\r\n\x00") || canonical == "Host" || canonical == "Connection" || canonical == "Content-Length" || canonical == "Transfer-Encoding" || canonical == "Accept-Encoding" || canonical == "Proxy-Authorization" {
			return fmt.Errorf("invalid HTTP readiness headers")
		}
		names[canonical] = true
		size += len(key) + len(value)
	}
	if len(h.Headers) > 16 || size > 8192 {
		return fmt.Errorf("HTTP readiness headers exceed bounds")
	}
	return nil
}
func validHTTPHeader(value string) bool {
	if value == "" {
		return false
	}
	for _, c := range value {
		if !(c >= 'a' && c <= 'z' || c >= 'A' && c <= 'Z' || c >= '0' && c <= '9' || strings.ContainsRune("!#$%&'*+-.^_`|~", c)) {
			return false
		}
	}
	return true
}

// The supervising probe process owns cancellation, deadlines and cleanup. This
// child only reads frozen data and makes one bounded request on host loopback.
func httpProbeAction(state, name, generation string) error {
	if !validName.MatchString(name) {
		return fmt.Errorf("invalid probe service")
	}
	var s Service
	if err := readJSON(filepath.Join(state, name+".command.json"), &s); err != nil {
		return err
	}
	if s.Generation != generation || s.Readiness == nil || s.Readiness.HTTPGet == nil || s.Readiness.Timeout < 1 || s.Readiness.Timeout > 600 {
		return fmt.Errorf("HTTP readiness generation or bounds changed")
	}
	ctx, cancel := context.WithTimeout(context.Background(), time.Duration(s.Readiness.Timeout)*time.Second)
	defer cancel()
	return checkHTTP(ctx, s.Readiness.HTTPGet)
}
func checkHTTP(ctx context.Context, h *HTTPProbe) error {
	if h.PendingEnvironment {
		return fmt.Errorf("HTTP readiness environment has not been admitted")
	}
	if err := validateHTTPProbe(h); err != nil {
		return err
	}
	// Do not inherit proxies, follow redirects, or decompress unbounded bodies.
	transport := &http.Transport{DisableCompression: true, MaxResponseHeaderBytes: 8192}
	defer transport.CloseIdleConnections()
	client := &http.Client{Transport: transport, CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}
	request, err := http.NewRequestWithContext(ctx, "GET", fmt.Sprintf("http://127.0.0.1:%d%s", h.Port, h.Path), nil)
	if err != nil {
		return fmt.Errorf("invalid HTTP readiness request")
	}
	for key, value := range h.Headers {
		request.Header.Set(key, value)
	}
	response, err := client.Do(request)
	if err != nil {
		return fmt.Errorf("HTTP readiness request failed or timed out")
	}
	defer response.Body.Close()
	if response.StatusCode != h.StatusCode {
		return fmt.Errorf("HTTP readiness returned status %d; expected %d", response.StatusCode, h.StatusCode)
	}
	if h.Body == nil {
		return nil
	}
	body, err := io.ReadAll(io.LimitReader(response.Body, maxProbeBody+1))
	if err != nil {
		return fmt.Errorf("HTTP readiness body could not be read")
	}
	if len(body) > maxProbeBody {
		return fmt.Errorf("HTTP readiness body exceeds 64 KiB")
	}
	actual := string(body)
	if h.TrimBody {
		actual = strings.TrimSpace(actual)
	}
	if actual != *h.Body {
		return fmt.Errorf("HTTP readiness body did not match")
	}
	return nil
}

// A compatible controller retains all service templates. A newly selected
// service receives this operation's resolved probe before it can start. Keep
// existing service/container identities and every live client's probe intact.
// The caller holds the scope gate; the admission lock excludes in-flight probes.
func admitHTTPProbes(saved *Plan, incoming Plan, selected []string, used map[string]bool) error {
	for _, name := range selected {
		next := incoming.Services[name].Readiness
		if used[name] || next == nil || next.HTTPGet == nil {
			continue
		}
		service, ok := saved.Services[name]
		if !ok || service.Readiness == nil || service.Readiness.HTTPGet == nil || next.HTTPGet.PendingEnvironment {
			return fmt.Errorf("HTTP readiness template changed for %s", name)
		}
		probe := *service.Readiness
		probe.HTTPGet = next.HTTPGet
		service.Readiness = &probe
		admission, err := locked(filepath.Join(saved.State, name+".admission"), false)
		if err != nil {
			return err
		}
		var record Service
		path := filepath.Join(saved.State, name+".command.json")
		err = readJSON(path, &record)
		if err == nil && record.Generation != saved.Generation {
			err = fmt.Errorf("HTTP readiness generation changed before admission")
		}
		if err == nil {
			record.Readiness = &probe
			err = atomic(path, record)
		}
		admission.Close()
		if err != nil {
			return err
		}
		saved.Services[name] = service
	}
	return nil
}
