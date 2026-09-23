package main

import (
	"os"
	"path/filepath"
	"syscall"
	"testing"
	"time"
)

func TestConsentRejectsEscapingLinksAndNonregularMessages(t *testing.T) {
	directory := t.TempDir()
	root, err := os.OpenRoot(directory)
	if err != nil {
		t.Fatal(err)
	}
	defer root.Close()
	outside := filepath.Join(t.TempDir(), "private")
	if err := os.WriteFile(outside, []byte("private"), 0600); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(outside, filepath.Join(directory, "question")); err != nil {
		t.Fatal(err)
	}
	if _, err := consentRead(root, "question", 1024); err == nil {
		t.Fatal("followed escaping link")
	}
	if err := syscall.Mkfifo(filepath.Join(directory, "pipe"), 0600); err != nil {
		t.Fatal(err)
	}
	if _, err := consentRead(root, "pipe", 1024); err == nil {
		t.Fatal("accepted FIFO")
	}
	if err := consentWrite(root, "question", "public"); err != nil {
		t.Fatal(err)
	}
	data, _ := os.ReadFile(outside)
	if string(data) != "private" {
		t.Fatal("modified outside file")
	}
}

func TestConsentLeaseExpiresAndRequiresNewHeartbeat(t *testing.T) {
	root, err := os.OpenRoot(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	defer root.Close()
	if err := consentWrite(root, "alive", "1"); err != nil {
		t.Fatal(err)
	}
	lease := &consentLease{}
	if !lease.alive(root, "alive") {
		t.Fatal("fresh owner missing")
	}
	lease.changed = time.Now().Add(-11 * time.Second)
	if lease.alive(root, "alive") {
		t.Fatal("stale owner accepted")
	}
	if err := consentWrite(root, "alive", "2"); err != nil {
		t.Fatal(err)
	}
	if !lease.alive(root, "alive") {
		t.Fatal("heartbeat change ignored")
	}
}
