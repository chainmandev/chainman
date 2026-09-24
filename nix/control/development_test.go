package main

import (
	"os"
	"path/filepath"
	"strings"
	"syscall"
	"testing"
	"time"
)

func TestDevelopmentOutputModes(t *testing.T) {
	for _, tty := range []bool{true, false} {
		for _, mode := range []string{"", "auto", "summary", "logs"} {
			got, err := developmentMode(mode, tty)
			want := mode == "summary" || ((mode == "" || mode == "auto") && tty)
			if err != nil || got != want {
				t.Fatal(mode, tty, got, err)
			}
		}
	}
	if _, err := developmentMode("quiet", true); err == nil {
		t.Fatal("accepted unknown mode")
	}
}

func TestDevelopmentPresentationAndBoundedChannel(t *testing.T) {
	p := Presentation{Title: "App", URLs: map[string]string{"Browser": "http://localhost:4321"}}
	if err := p.validate(); err != nil {
		t.Fatal(err)
	}
	for _, value := range []string{"file:///tmp/example", "http://user:secret@localhost", "http://localhost/\x1b[2J"} {
		p.URLs["Browser"] = value
		if p.validate() == nil {
			t.Fatal("accepted unsafe display", value)
		}
	}
	path := filepath.Join(t.TempDir(), "progress.json")
	for _, value := range []string{`{"operation":"other","phase":"ready"}`, `{"operation":"id","phase":"stop"}`, strings.Repeat("x", 2048)} {
		os.WriteFile(path, []byte(value), 0600)
		if readProgress(path, "id") != "" {
			t.Fatal("accepted invalid progress")
		}
	}
	os.WriteFile(path, []byte(`{"operation":"id","phase":"ready"}`), 0600)
	if readProgress(path, "id") != "ready" {
		t.Fatal("lost progress")
	}
	os.Rename(path, path+".target")
	os.Symlink(path+".target", path)
	if readProgress(path, "id") != "" {
		t.Fatal("followed channel symlink")
	}
	os.Remove(path)
	syscall.Mkfifo(path, 0600)
	if readProgress(path, "id") != "" {
		t.Fatal("read channel FIFO")
	}
}

func TestDevelopmentLifecycleAndConcurrentClients(t *testing.T) {
	t.Setenv("CHAINMAN_DEV_OUTPUT", "summary")
	state := physicalTempDir(t)
	p := Plan{State: state, WaitForServices: true, Presentation: Presentation{Task: "dev", Title: "Application", URLs: map[string]string{"Browser": "http://localhost:1234"}}}
	first, err := beginDevelopment(&p)
	if err != nil {
		t.Fatal(err)
	}
	q := p
	q.Task.Environment = nil
	second, err := beginDevelopment(&q)
	if err != nil {
		t.Fatal(err)
	}
	if first.row.ID == second.row.ID || first.channel == second.channel {
		t.Fatal("clients share status identity")
	}
	if !strings.Contains(developmentSummary(first.row, false), "http://localhost:1234") || first.row.ReachedReady {
		t.Fatal("URL not available before preparation")
	}
	if err := atomic(filepath.Join(first.channel, "progress.json"), map[string]string{"operation": first.row.ID, "phase": "ready"}); err != nil {
		t.Fatal(err)
	}
	deadline := time.Now().Add(3 * time.Second)
	for {
		first.mu.Lock()
		ready := first.row.ReachedReady
		first.mu.Unlock()
		if ready {
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("completion was not observed")
		}
		time.Sleep(10 * time.Millisecond)
	}
	first.services(Plan{State: state, Services: map[string]Service{"server": {Readiness: &Probe{}}}}, []Process{{Name: "server", Running: true, Ready: "NotReady"}}, map[string]bool{"server": true})
	time.Sleep(300 * time.Millisecond)
	first.mu.Lock()
	phase := first.row.Phase
	first.mu.Unlock()
	if phase != "degraded" {
		t.Fatal("unready service was not observed", phase)
	}
	first.finish(130)
	second.finish(7)
	rows, err := applicationStatuses(state)
	if err != nil || len(rows) != 2 {
		t.Fatal(rows, err)
	}
	for _, row := range rows {
		if !terminalPhase(row.Phase) {
			t.Fatal("cancelled/failed client remained live", row)
		}
		if row.ID == second.row.ID && row.ReachedReady {
			t.Fatal("failed preparation became ready")
		}
	}
	if _, err := os.Stat(first.channel); !os.IsNotExist(err) {
		t.Fatal("channel retained", err)
	}
	// A killed owner cannot leave a historical ready record looking current.
	row := rows[0]
	row.Phase, row.Owner = "ready", Identity{}
	atomic(filepath.Join(state, "applications", row.ID+".json"), row)
	rows, err = applicationStatuses(state)
	if err != nil || rows[0].Phase != "stopped" {
		t.Fatal(rows, err)
	}
}

func TestDevelopmentBuildFailureGenerationAndDiagnostics(t *testing.T) {
	state := physicalTempDir(t)
	p := Plan{State: state, Generation: "current", Requested: []string{"app"}, Services: map[string]Service{"app": {Watch: &Watch{}}}}
	atomic(filepath.Join(state, "plan.json"), p)
	path := filepath.Join(state, "app.build-result.json")
	atomic(path, map[string]string{"generation": "old", "error": "failed"})
	if len(developmentProblems(p)) != 0 {
		t.Fatal("stale failure reported")
	}
	atomic(path, map[string]string{"generation": "current", "error": "failed"})
	if len(developmentProblems(p)) != 1 {
		t.Fatal("failed rebuild overlooked")
	}
	atomic(path, map[string]string{"generation": "current"})
	if len(developmentProblems(p)) != 0 {
		t.Fatal("successful rebuild remains failed")
	}
	var tail diagnosticTail
	tail.Write([]byte("old diagnostic\n"))
	for i := 0; i < 1024; i++ {
		tail.Write([]byte(strings.Repeat("x", 1024) + "\n"))
	}
	tail.Write([]byte("current diagnostic\n"))
	if len(tail.data) > logHistoryBytes || len(tail.excerpt()) > 16*1024 || strings.Contains(tail.excerpt(), "old diagnostic") || !strings.Contains(tail.excerpt(), "current diagnostic") {
		t.Fatal("diagnostic bound failed")
	}
}
