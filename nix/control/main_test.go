package main

import (
	"os"
	"path/filepath"
	"strings"
	"syscall"
	"testing"
)

func TestKernelIdentityAndGroupInventory(t *testing.T) {
	identity, err := identify(os.Getpid())
	if err != nil {
		t.Fatal(err)
	}
	if !identity.alive() {
		t.Fatal("current process was not identified")
	}
	identity.Birth += "stale"
	if identity.alive() {
		t.Fatal("a reused PID would be accepted")
	}
	members, err := groupMembers(syscall.Getpgrp())
	if err != nil {
		t.Fatal(err)
	}
	found := false
	for _, pid := range members {
		if pid == os.Getpid() {
			found = true
		}
	}
	if !found {
		t.Fatal("kernel group inventory omitted the caller")
	}
}
func TestScopeCannotFollowSymlink(t *testing.T) {
	base := t.TempDir()
	real := filepath.Join(base, "real")
	if err := os.Mkdir(real, 0700); err != nil {
		t.Fatal(err)
	}
	link := filepath.Join(base, "link")
	if err := os.Symlink(real, link); err != nil {
		t.Fatal(err)
	}
	if private(filepath.Join(link, "state")) == nil {
		t.Fatal("linked scope was accepted")
	}
	if _, err := os.Stat(filepath.Join(real, "state")); !os.IsNotExist(err) {
		t.Fatal("private validation wrote through the link")
	}
}
func TestNewChildDoesNotAdvertiseClosedOperationDescriptors(t *testing.T) {
	names := []string{"TOOLCHAIN_LOCK_FD", "TOOLCHAIN_GATE_FD", "TOOLCHAIN_COMPAT_FD", "TOOLCHAIN_OPERATION_ID", "TOOLCHAIN_ANCESTOR_FDS", "CHAINMAN_COMPILER_OWNER"}
	for _, name := range names {
		t.Setenv(name, "999")
	}
	cmd, err := child(Command{Argv: []string{"true"}})
	if err != nil {
		t.Fatal(err)
	}
	for _, entry := range cmd.Env {
		for _, name := range names {
			if strings.HasPrefix(entry, name+"=") {
				t.Fatalf("forwarded stale %s", name)
			}
		}
	}
}
func TestCachedExecutableTamperingIsRejected(t *testing.T) {
	base := t.TempDir()
	backend := filepath.Join(base, "backend")
	if err := os.WriteFile(backend, []byte("fixture backend"), 0700); err != nil {
		t.Fatal(err)
	}
	p := Plan{State: base, Backend: backend}
	if _, err := persistTools(&p); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(p.Backend, []byte("tampered"), 0700); err != nil {
		t.Fatal(err)
	}
	p.Backend = backend
	if _, err := persistTools(&p); err == nil {
		t.Fatal("modified executable cache was accepted")
	}
}
