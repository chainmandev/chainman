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
	base := t.TempDir()
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
