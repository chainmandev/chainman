package main

import (
	"bytes"
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"testing"
)

func leaseTestState(t *testing.T) string {
	t.Helper()
	state := filepath.Join(physicalTempDir(t), "state")
	if err := private(state); err != nil {
		t.Fatal(err)
	}
	return state
}

func TestTaskIdentityPreservesLeaseInodeAndBytes(t *testing.T) {
	state := leaseTestState(t)
	path := filepath.Join(state, "client.lease")
	if err := atomic(path, Lease{Services: []string{"worker"}, TaskReceipt: true}); err != nil {
		t.Fatal(err)
	}
	lease, err := locked(path, false)
	if err != nil {
		t.Fatal(err)
	}
	defer lease.Close()
	before, _ := os.ReadFile(path)
	inode, _ := lease.Stat()
	if err = publishLeaseTask(path, lease); err != nil {
		t.Fatal(err)
	}
	after, _ := os.ReadFile(path)
	current, _ := os.Stat(path)
	if !bytes.Equal(before, after) || !os.SameFile(inode, current) {
		t.Fatal("task publication rewrote or replaced the locked lease")
	}
	// The identity must retain both local and repository leases after an entry
	// shell closes its inherited descriptor and the caller dies.
	lease.Close()
	used, err := active(Plan{State: state})
	if err != nil || !used["worker"] {
		t.Fatalf("published task lost local lease: %v, %v", used, err)
	}
	resource := Plan{State: filepath.Join(filepath.Dir(state), "repository")}
	live, err := parentAlive(resource, LeaseRef{State: state, File: "client.lease"})
	if err != nil || !live {
		t.Fatalf("published task lost repository lease: %v, %v", live, err)
	}
	if err = atomic(path+".task.json", Identity{PID: os.Getpid(), Birth: "stale"}); err != nil {
		t.Fatal(err)
	}
	if err = atomic(path+".command.json", Command{Argv: []string{"true"}}); err != nil {
		t.Fatal(err)
	}
	used, err = active(Plan{State: state})
	if err != nil || len(used) != 0 {
		t.Fatalf("dead task lease survived: %v, %v", used, err)
	}
	for _, suffix := range []string{"", ".task.json", ".command.json"} {
		if _, err = os.Stat(path + suffix); !os.IsNotExist(err) {
			t.Fatalf("stale task file retained: %s: %v", suffix, err)
		}
	}
}

func TestTaskIdentityPublicationFailureLeavesValidLease(t *testing.T) {
	state := leaseTestState(t)
	path := filepath.Join(state, "client.lease")
	if err := atomic(path, Lease{Services: []string{"worker"}, TaskReceipt: true}); err != nil {
		t.Fatal(err)
	}
	lease, err := locked(path, false)
	if err != nil {
		t.Fatal(err)
	}
	defer lease.Close()
	before, _ := os.ReadFile(path)
	if err = os.Mkdir(path+".task.json", 0700); err != nil {
		t.Fatal(err)
	}
	if err = publishLeaseTask(path, lease); err == nil {
		t.Fatal("identity publication unexpectedly succeeded")
	}
	after, _ := os.ReadFile(path)
	if !bytes.Equal(before, after) {
		t.Fatal("failed identity publication changed lease bytes")
	}
	used, err := active(Plan{State: state})
	if err != nil || !used["worker"] {
		t.Fatalf("live descriptor lost its lease: %v, %v", used, err)
	}
	lease.Close()
	if _, err = active(Plan{State: state}); err == nil {
		t.Fatal("unreadable identity was treated as a dead task")
	}
	if _, err = os.Stat(path); err != nil {
		t.Fatal("uncertain ownership discarded the receipt")
	}
}

func TestTaskPublicationRejectsRemovedOrReplacedLease(t *testing.T) {
	path := filepath.Join(leaseTestState(t), "client.lease")
	if err := atomic(path, Lease{TaskReceipt: true}); err != nil {
		t.Fatal(err)
	}
	lease, err := locked(path, false)
	if err != nil {
		t.Fatal(err)
	}
	defer lease.Close()
	if err = os.Remove(path); err != nil {
		t.Fatal(err)
	}
	if err = publishLeaseTask(path, lease); err == nil {
		t.Fatal("removed lease admitted a task")
	}
	if err = atomic(path, Lease{TaskReceipt: true}); err != nil {
		t.Fatal(err)
	}
	if err = publishLeaseTask(path, lease); err == nil {
		t.Fatal("replacement lease admitted the old task")
	}
	if _, err = os.Stat(path + ".task.json"); !os.IsNotExist(err) {
		t.Fatal("rejected task published an identity")
	}
}

func TestLeaseEntryHelper(t *testing.T) {
	if path := os.Getenv("CHAINMAN_TEST_LEASE_ENTRY"); path != "" {
		os.Exit(mainAction([]string{"leased-task", path}))
	}
}

func TestLeaseEnvironmentHelper(t *testing.T) {
	if os.Getenv("CHAINMAN_TEST_ENVIRONMENT_OUTPUT") != "1" {
		return
	}
	values := map[string]string{}
	for _, name := range []string{"CHAINMAN_TEST_OVERRIDE", "CHAINMAN_SERVICE_LEASE_FDS"} {
		values[name] = os.Getenv(name)
	}
	if err := json.NewEncoder(os.Stdout).Encode(values); err != nil {
		os.Exit(1)
	}
	os.Exit(0)
}

func TestLeaseEntryAppliesDeclaredEnvironmentOverrides(t *testing.T) {
	t.Setenv("CHAINMAN_TEST_OVERRIDE", "inherited")
	t.Setenv("CHAINMAN_SERVICE_LEASE_FDS", "[999]")
	path := filepath.Join(leaseTestState(t), "client.lease")
	if err := atomic(path, Lease{TaskReceipt: true}); err != nil {
		t.Fatal(err)
	}
	lease, err := locked(path, false)
	if err != nil {
		t.Fatal(err)
	}
	defer lease.Close()
	want := map[string]string{
		"CHAINMAN_TEST_OVERRIDE":     "declared with spaces $()",
		"CHAINMAN_SERVICE_LEASE_FDS": "[3]",
	}
	environment := map[string]string{"CHAINMAN_TEST_ENVIRONMENT_OUTPUT": "1"}
	for name, value := range want {
		environment[name] = value
	}
	command := Command{
		Argv:        []string{os.Args[0], "-test.run=^TestLeaseEnvironmentHelper$"},
		Environment: environment,
	}
	if err = atomic(path+".command.json", command); err != nil {
		t.Fatal(err)
	}
	cmd := exec.Command(os.Args[0], "-test.run=^TestLeaseEntryHelper$")
	cmd.Env = append(os.Environ(), "CHAINMAN_TEST_LEASE_ENTRY="+path)
	cmd.ExtraFiles = []*os.File{lease}
	output, err := cmd.CombinedOutput()
	if err != nil {
		t.Fatalf("task entry failed: %s, %v", output, err)
	}
	var got map[string]string
	if err = json.Unmarshal(output, &got); err != nil {
		t.Fatalf("invalid task output: %s, %v", output, err)
	}
	for name, value := range want {
		if got[name] != value {
			t.Errorf("%s: got %q, want %q", name, got[name], value)
		}
	}
}

func TestLeaseEntryRefusesBeforeProjectSideEffects(t *testing.T) {
	for _, failure := range []string{"missing-lease", "replaced-lease", "identity-write"} {
		t.Run(failure, func(t *testing.T) {
			state := leaseTestState(t)
			path := filepath.Join(state, "client.lease")
			if err := atomic(path, Lease{TaskReceipt: true}); err != nil {
				t.Fatal(err)
			}
			lease, err := locked(path, false)
			if err != nil {
				t.Fatal(err)
			}
			defer lease.Close()
			command := Command{Argv: []string{"sh", "-c", "touch project-ran"}, Directory: state}
			if err = atomic(path+".command.json", command); err != nil {
				t.Fatal(err)
			}
			switch failure {
			case "missing-lease":
				err = os.Remove(path)
			case "replaced-lease":
				err = atomic(path, Lease{TaskReceipt: true})
			case "identity-write":
				err = os.Mkdir(path+".task.json", 0700)
			}
			if err != nil {
				t.Fatal(err)
			}
			cmd := exec.Command(os.Args[0], "-test.run=^TestLeaseEntryHelper$")
			cmd.Env = append(os.Environ(), "CHAINMAN_TEST_LEASE_ENTRY="+path)
			cmd.ExtraFiles = []*os.File{lease}
			output, err := cmd.CombinedOutput()
			if err == nil {
				t.Fatalf("invalid task entry succeeded: %s", output)
			}
			if _, err = os.Stat(filepath.Join(state, "project-ran")); !os.IsNotExist(err) {
				t.Fatal("project command ran before durable identity publication")
			}
		})
	}
}

func TestPreviousTaskIdentityRemainsReadable(t *testing.T) {
	identity, err := identify(os.Getpid())
	if err != nil {
		t.Fatal(err)
	}
	live, err := leaseTaskAlive("unused", Lease{Task: &identity})
	if err != nil || !live {
		t.Fatalf("previous task identity was lost: %v, %v", live, err)
	}
}
