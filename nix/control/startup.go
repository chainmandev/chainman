package main

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"syscall"
	"time"
)

// Timing is opt-in and contains no commands, paths or environment values.
func tracePhase(phase string) func() {
	if os.Getenv("CHAINMAN_TIMING") != "1" {
		return func() {}
	}
	started := time.Now()
	return func() {
		body, err := json.Marshal(map[string]any{
			"schema": 1, "phase": phase, "event": "end",
			"operation":  fmt.Sprintf("%d-%d", os.Getpid(), started.UnixNano()),
			"elapsed_ns": time.Since(started).Nanoseconds(),
		})
		if err == nil {
			fmt.Fprintln(os.Stderr, "CHAINMAN_TIMING "+string(body))
		}
	}
}

// Stop announces cancellation before waiting for the scope mutation gate. A
// durable ticket also reaches startup waiting in a shared repository resource.
// Process Compose still owns readiness; this only cancels its waiting client.
var validStopToken = regexp.MustCompile(`^[a-f0-9]{32}$`)

type stopNotice struct {
	Token   string `json:"token"`
	Pending bool   `json:"pending"`
}

func readStopNotice(state string) (stopNotice, error) {
	var notice stopNotice
	err := readJSON(filepath.Join(state, "startup-stop.json"), &notice)
	if os.IsNotExist(err) {
		return notice, nil
	}
	if err == nil && !validStopToken.MatchString(notice.Token) {
		err = fmt.Errorf("invalid startup stop ticket")
	}
	return notice, err
}

type startupInterrupted struct{ signal syscall.Signal }

func (e *startupInterrupted) Error() string { return "startup interrupted: " + e.signal.String() }

type startupGuard struct {
	signals <-chan os.Signal
	stops   map[string]string
}

func (g *startupGuard) observe(state string) error {
	notice, err := readStopNotice(state)
	if err != nil {
		return err
	}
	if notice.Pending {
		return servicesStopped
	}
	g.stops[state] = notice.Token
	return g.check()
}

func (g *startupGuard) check() error {
	select {
	case sig := <-g.signals:
		return &startupInterrupted{sig.(syscall.Signal)}
	default:
	}
	for state, token := range g.stops {
		notice, err := readStopNotice(state)
		if err != nil {
			return err
		}
		if notice.Pending || notice.Token != token {
			return servicesStopped
		}
	}
	return nil
}

func (g *startupGuard) lock(path string) (*os.File, error) {
	for {
		if err := g.check(); err != nil {
			return nil, err
		}
		file, err := locked(path, true)
		if !errors.Is(err, syscall.EWOULDBLOCK) {
			return file, err
		}
		time.Sleep(50 * time.Millisecond)
	}
}
