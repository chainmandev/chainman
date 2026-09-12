package main

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"syscall"
	"testing"
)

func TestHTTPReadinessRejectsInvalidPlans(t *testing.T) {
	root := t.TempDir()
	command := Command{Argv: []string{"/bin/true"}, Directory: root}
	p := Plan{Schema: 1, Root: root, State: t.TempDir(), Backend: "/bin/true", Fingerprint: "fixture", Services: map[string]Service{}, Requested: []string{"web"}, Task: command}
	for _, h := range []HTTPProbe{
		{8080, "/health?ready=1", 204},
		{0, "/", 200}, {65536, "/", 200},
		{80, "//remote.example/", 200}, {80, "http://remote.example/", 200},
		{80, "/health#fragment", 200}, {80, "/", 404},
	} {
		p.Services["web"] = Service{Command: command, Restart: "no", Shutdown: 1, Readiness: &Probe{HTTPGet: &h, Period: 1, Timeout: 1, Failures: 2}}
		err := validate(p)
		if (err == nil) != (h.Port == 8080) {
			t.Fatalf("HTTP probe %+v: %v", h, err)
		}
	}
	s := p.Services["web"]
	s.Readiness.HTTPGet = &HTTPProbe{8080, "/", 200}
	s.Readiness.Command = command
	p.Services["web"] = s
	if validate(p) == nil {
		t.Fatal("ambiguous command and HTTP readiness accepted")
	}
}

func TestContainerOutlivesClientDescriptorLease(t *testing.T) {
	state := t.TempDir()
	engine := filepath.Join(state, "engine")
	owner := &Container{Engine: engine, Name: "chainman-fixture", Token: "0123456789abcdef0123456789abcdef"}
	receipt := filepath.Join(state, "client.lease")
	respond := func(running bool, label string) {
		t.Helper()
		data, _ := json.Marshal([]any{map[string]any{"Id": "immutable-fixture-id", "State": map[string]bool{"Running": running}, "Config": map[string]any{"Labels": map[string]string{"dev.chainman.owner": label}}}})
		if e := os.WriteFile(engine, []byte("#!/bin/sh\nprintf '%s\\n' "+quote(string(data))+"\n"), 0700); e != nil {
			t.Fatal(e)
		}
	}
	respond(true, owner.Token)
	if e := atomic(receipt, Lease{Services: []string{"database"}, Container: owner}); e != nil {
		t.Fatal(e)
	}
	p := Plan{State: state}
	used, e := active(p)
	if e != nil {
		t.Fatal(e)
	}
	if !used["database"] {
		t.Fatal("live task container lost its service lease")
	}
	respond(true, "replacement-token")
	if _, e = active(p); e == nil {
		t.Fatal("replacement container was accepted as the lease owner")
	}
	if _, e = os.Stat(receipt); e != nil {
		t.Fatal("uncertain ownership discarded the lease")
	}
	if e = os.WriteFile(engine, []byte("#!/bin/sh\nexit 1\n"), 0700); e != nil {
		t.Fatal(e)
	}
	if _, e = active(p); e == nil {
		t.Fatal("engine outage discarded ownership")
	}
	respond(false, owner.Token)
	used, e = active(p)
	if e != nil {
		t.Fatal(e)
	}
	if used["database"] {
		t.Fatal("finished task container retained its lease")
	}
	if _, e = os.Stat(receipt); !os.IsNotExist(e) {
		t.Fatal("stale lease was not collected")
	}
}

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

func TestChangedEngineContextCannotInspectOrStopContainers(t *testing.T) {
	root := t.TempDir()
	engine := filepath.Join(root, "docker")
	marker := filepath.Join(root, "container-action")
	body := "#!/bin/sh\nif [ \"$1\" = info ]; then printf '%s\\n' changed-daemon; else touch " + quote(marker) + "; fi\n"
	if e := os.WriteFile(engine, []byte(body), 0700); e != nil {
		t.Fatal(e)
	}
	owner := &Container{Engine: engine, Name: "chainman-fixture", Token: "0123456789abcdef0123456789abcdef", EngineIdentity: "original-daemon"}
	if _, e := inspectContainer(owner); e == nil {
		t.Fatal("changed daemon accepted")
	}
	if e := stopContainer(owner, 1); e == nil {
		t.Fatal("changed daemon accepted during stop")
	}
	if _, e := os.Stat(marker); !os.IsNotExist(e) {
		t.Fatal("a container command reached the changed daemon")
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
