package main

import (
	"bytes"
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestLogCursorRotationTruncationAndBoundedHistory(t *testing.T) {
	path := filepath.Join(t.TempDir(), "services.log")
	c := &logCursor{path: path}
	defer c.close()
	var out bytes.Buffer
	write := func(value string) {
		t.Helper()
		if e := os.WriteFile(path, []byte(value), 0600); e != nil {
			t.Fatal(e)
		}
	}
	poll := func(want string) {
		t.Helper()
		out.Reset()
		if e := c.copy(&out); e != nil {
			t.Fatal(e)
		}
		if out.String() != want {
			t.Fatalf("got %q; want %q", out.String(), want)
		}
	}
	poll("")
	write(strings.Repeat("x", logHistoryBytes) + "initial\n")
	poll(strings.Repeat("x", logHistoryBytes-len("initial\n")) + "initial\n")
	poll("")
	f, e := os.OpenFile(path, os.O_APPEND|os.O_WRONLY, 0600)
	if e != nil {
		t.Fatal(e)
	}
	f.WriteString("appended\n")
	f.Close()
	if e = os.Rename(path, path+".1"); e != nil {
		t.Fatal(e)
	}
	write("rotated\n")
	poll("appended\nrotated\n")
	write("short\n")
	poll("short\n")
	poll("")
	if e = os.Remove(path); e != nil {
		t.Fatal(e)
	}
	if e = os.Symlink(path+".1", path); e != nil {
		t.Fatal(e)
	}
	if e = c.copy(&out); e == nil {
		t.Fatal("accepted symlink log")
	}
}

func TestLogsUseOnlySelectedScopesAndNeverLeaseServices(t *testing.T) {
	base := physicalTempDir(t)
	state, resource, unrelated := filepath.Join(base, "worktree"), filepath.Join(base, "resource"), filepath.Join(base, "unrelated")
	for _, path := range []string{state, resource, unrelated} {
		if e := os.Mkdir(path, 0700); e != nil {
			t.Fatal(e)
		}
		if e := os.WriteFile(filepath.Join(path, "services.log"), []byte(filepath.Base(path)+"\n"), 0600); e != nil {
			t.Fatal(e)
		}
	}
	p := Plan{State: state, Resources: []Plan{{State: resource}}}
	var out bytes.Buffer
	if e := streamLogs(context.Background(), p, &out, false); e != nil {
		t.Fatal(e)
	}
	if out.String() != "worktree\nresource\n" {
		t.Fatal(out.String())
	}
	for _, path := range []string{state, resource} {
		files, e := os.ReadDir(path)
		if e != nil {
			t.Fatal(e)
		}
		if len(files) != 1 {
			t.Fatal("viewer created ownership state", files)
		}
	}
	p.Resources[0].State = filepath.Join(base, "outside", "resource")
	if _, e := logCursors(p); e == nil {
		t.Fatal("accepted unrelated resource scope")
	}
}

func TestLiveLogsStartBeforeAcquisitionWithoutReplayingHistory(t *testing.T) {
	for _, existing := range []bool{false, true} {
		state := physicalTempDir(t)
		os.Chmod(state, 0700)
		if err := os.Chmod(state, 0700); err != nil {
			t.Fatal(err)
		}
		path := filepath.Join(state, "services.log")
		if existing {
			os.WriteFile(path, []byte("old failure\n"), 0600)
		}
		cursors, err := liveLogCursors(Plan{State: state})
		if err != nil {
			t.Fatal(err)
		}
		f, err := os.OpenFile(path, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0600)
		if err != nil {
			t.Fatal(err)
		}
		f.WriteString("current startup\n")
		f.Close()
		var out bytes.Buffer
		if err := streamLogCursors(context.Background(), cursors, &out, false); err != nil {
			t.Fatal(err)
		}
		if out.String() != "current startup\n" {
			t.Fatal(out.String())
		}
	}
}

func TestLogRendererDistinguishesStreamsAndRetainsRealErrors(t *testing.T) {
	var out bytes.Buffer
	r := logRenderer{destination: &out}
	input := "{\"level\":\"error\",\"process\":\"postgres\",\"time\":\"now\",\"message\":\"LOG: ready\"}\n" +
		"{\"level\":\"error\",\"process\":\"postgres\",\"message\":\"ERROR: deadlock detected\"}\n" +
		"{\"level\":\"info\",\"process\":\"app\",\"message\":\"hello\"}\n" +
		"historical plain text\n{\"level\":\"error\",\"message\":\"controller failed\"}\n"
	for _, b := range []byte(input) {
		if _, err := r.Write([]byte{b}); err != nil {
			t.Fatal(err)
		}
	}
	want := "now [postgres stderr] LOG: ready\n [postgres stderr] ERROR: deadlock detected\n [app stdout] hello\n" +
		"historical plain text\n{\"level\":\"error\",\"message\":\"controller failed\"}\n"
	if out.String() != want {
		t.Fatalf("got %q", out.String())
	}
}

func TestStartupFailureTailOmitsHistoryAndRetainsLatestError(t *testing.T) {
	for _, rotate := range []bool{false, true} {
		state := physicalTempDir(t)
		os.Chmod(state, 0700)
		if err := private(state); err != nil {
			t.Fatal(err)
		}
		path := filepath.Join(state, "services.log")
		os.WriteFile(path, []byte("STALE_FAILURE\n"), 0600)
		cursors, err := liveLogCursors(Plan{State: state})
		if err != nil {
			t.Fatal(err)
		}
		c := cursors[0]
		defer c.close()
		var empty bytes.Buffer
		if err := c.latest(&empty); err != nil || empty.Len() != 0 {
			t.Fatal("replayed history", err)
		}
		if rotate {
			os.Rename(path, path+".old")
		}
		f, err := os.OpenFile(path, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0600)
		if err != nil {
			t.Fatal(err)
		}
		f.WriteString(strings.Repeat("startup noise\n", logHistoryBytes) + "CURRENT_FAILURE\n")
		f.Close()
		var out bytes.Buffer
		if err := c.latest(&out); err != nil {
			t.Fatal(err)
		}
		if !strings.HasSuffix(out.String(), "CURRENT_FAILURE\n") || strings.Contains(out.String(), "STALE_FAILURE") || out.Len() > logHistoryBytes {
			t.Fatal("incorrect bounded tail", out.Len())
		}
	}
}
