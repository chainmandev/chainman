package main

import (
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"syscall"
	"testing"
	"time"
)

func verificationFixture(t *testing.T, names ...string) (VerificationPlan, string) {
	t.Helper()
	root := physicalTempDir(t)
	output := physicalTempDir(t)
	if err := os.Chmod(output, 0700); err != nil {
		t.Fatal(err)
	}
	entry := filepath.Join(root, "entry")
	identity := VerificationIdentity{"fixture", strings.Repeat("a", 64), strings.Repeat("b", 40)}
	body, _ := json.Marshal(identity)
	state := filepath.Join(root, "service-state")
	scope, _ := json.Marshal(state)
	health := `{"state":` + string(scope) + `,"complete":true,"busy":false,"recovery_required":false,"running":false,"leases":{},"inspection_errors":[],"resources":[]}`
	script := "#!/bin/sh\nset -eu\ncase \"$1\" in\n" +
		"_verification-identity) if [ -e changed ]; then printf '{}'; else printf '%s' " + quote(string(body)) + "; fi ;;\n" +
		"status) if [ -e unsafe ]; then printf '{}'; else printf '%s' " + quote(health) + "; fi ;;\n" +
		"run) printf '%s\\n' \"$2\" >> events; printf 'output %s\\n' \"$2\"; case \"$2\" in\n" +
		"fail) exit 7 ;; killed) exit 137 ;; unsafe) touch unsafe; exit 8 ;; mutate) touch changed ;;\n" +
		"slow) trap 'exit 0' TERM INT HUP; touch ready; while :; do sleep 1; done ;;\n" +
		"esac ;; *) exit 2 ;; esac\n"
	if err := os.WriteFile(entry, []byte(script), 0700); err != nil {
		t.Fatal(err)
	}
	plan := VerificationPlan{Schema: 1, Root: root, Identity: identity,
		IdentityCommand: []string{entry, "_verification-identity"}, HealthCommand: []string{entry, "status", state}}
	for _, name := range names {
		plan.Gates = append(plan.Gates, VerificationGate{Name: name, Argv: []string{entry, "run", name}})
	}
	return plan, output
}
func verificationReadReport(t *testing.T, directory string) VerificationReport {
	t.Helper()
	var report VerificationReport
	if err := readJSON(filepath.Join(directory, "report.json"), &report); err != nil {
		t.Fatal(err)
	}
	return report
}
func TestVerificationCollectsFailuresAndBlocksDependents(t *testing.T) {
	plan, output := verificationFixture(t, "fail", "blocked", "last")
	plan.Gates[1].Dependencies = []string{"fail"}
	if err := verificationValidate(plan); err != nil {
		t.Fatal(err)
	}
	if code := verificationRun(plan, output); code != 1 {
		t.Fatalf("code %d", code)
	}
	report := verificationReadReport(t, output)
	for i, want := range []string{"failed", "blocked", "passed"} {
		if report.Gates[i].Status != want {
			t.Fatalf("gate %+v, want %s", report.Gates[i], want)
		}
	}
	events, _ := os.ReadFile(filepath.Join(plan.Root, "events"))
	if string(events) != "fail\nlast\n" {
		t.Fatalf("events %q", events)
	}
	log, err := os.ReadFile(report.Gates[0].Log)
	if err != nil || !strings.Contains(string(log), "output fail") {
		t.Fatalf("log %q %v", log, err)
	}
	if report.Code != 1 || report.Ended == nil || *report.Gates[0].Code != 7 {
		t.Fatalf("report %+v", report)
	}
}
func TestVerificationAllGreen(t *testing.T) {
	plan, output := verificationFixture(t, "first", "last")
	if code := verificationRun(plan, output); code != 0 {
		t.Fatalf("code %d", code)
	}
	report := verificationReadReport(t, output)
	if report.Code != 0 || report.Identity != plan.Identity {
		t.Fatalf("report %+v", report)
	}
}
func TestVerificationStopsOnUnsafeCleanupAndSourceDrift(t *testing.T) {
	for _, name := range []string{"unsafe", "mutate"} {
		t.Run(name, func(t *testing.T) {
			plan, output := verificationFixture(t, name, "last")
			if code := verificationRun(plan, output); code != 2 {
				t.Fatalf("code %d", code)
			}
			report := verificationReadReport(t, output)
			if report.Code != 2 || report.Reason == "" || report.Gates[1].Status != "unrun" {
				t.Fatalf("report %+v", report)
			}
		})
	}
}
func TestVerificationKilledGateCannotAuthorizeForce(t *testing.T) {
	plan, output := verificationFixture(t, "killed", "last")
	if code := verificationRun(plan, output); code != 137 {
		t.Fatalf("code %d", code)
	}
	report := verificationReadReport(t, output)
	if report.Reason != "interrupted" || report.Gates[0].Status != "interrupted" || report.Gates[1].Status != "unrun" {
		t.Fatalf("report %+v", report)
	}
}
func TestVerificationRejectsMalformedPlanAndUnsafeOutput(t *testing.T) {
	plan, output := verificationFixture(t, "first", "last")
	plan.Gates[0].Dependencies = []string{"last"}
	if verificationValidate(plan) == nil {
		t.Fatal("forward prerequisite accepted")
	}
	plan.Gates[0].Dependencies = nil
	plan.Gates[1].Name = "first"
	if verificationValidate(plan) == nil {
		t.Fatal("duplicate gate accepted")
	}
	plan.Gates[1].Name = "last"
	path := filepath.Join(output, "plan.json")
	if err := atomic(path, plan); err != nil {
		t.Fatal(err)
	}
	if code := verificationAction([]string{path, plan.Root}); code == 0 {
		t.Fatal("tracked report directory accepted")
	}
	link := filepath.Join(output, "link")
	if err := os.Symlink(output, link); err != nil {
		t.Fatal(err)
	}
	if code := verificationAction([]string{path, link}); code == 0 {
		t.Fatal("linked report directory accepted")
	}
}
func TestVerificationCancellationReportsAndDoesNotStartNextGate(t *testing.T) {
	plan, output := verificationFixture(t, "slow", "last")
	path := filepath.Join(output, "plan.json")
	if err := atomic(path, plan); err != nil {
		t.Fatal(err)
	}
	cmd := exec.Command(os.Args[0], "verification", path, output)
	if err := cmd.Start(); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if cmd.ProcessState == nil {
			cmd.Process.Kill()
			cmd.Wait()
		}
	})
	deadline := time.Now().Add(10 * time.Second)
	for {
		if _, err := os.Stat(filepath.Join(plan.Root, "ready")); err == nil {
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("gate did not start")
		}
		time.Sleep(10 * time.Millisecond)
	}
	if err := cmd.Process.Signal(syscall.SIGTERM); err != nil {
		t.Fatal(err)
	}
	done := make(chan error, 1)
	go func() { done <- cmd.Wait() }()
	select {
	case <-done:
	case <-time.After(15 * time.Second):
		t.Fatal("cancellation did not finish")
	}
	if cmd.ProcessState.ExitCode() != 143 {
		t.Fatalf("exit %d", cmd.ProcessState.ExitCode())
	}
	paths, _ := filepath.Glob(filepath.Join(output, "verification-*"))
	if len(paths) != 1 {
		t.Fatalf("report directories %v", paths)
	}
	report := verificationReadReport(t, paths[0])
	if report.Code != 143 || report.Gates[0].Status != "interrupted" || report.Gates[1].Status != "unrun" {
		t.Fatalf("report %+v", report)
	}
}

func TestVerificationHealthUsesPreparedNativeStatusWithoutColdLauncher(t *testing.T) {
	plan, output := verificationFixture(t, "first", "last")
	entry := plan.IdentityCommand[0]
	marker := filepath.Join(plan.Root, "cold-launcher-entered")
	cold := filepath.Join(plan.Root, "cold-entry")
	body := "#!/bin/sh\ncase \"$1\" in status|services-status) touch " + quote(marker) + "; exit 91 ;; esac\nexec " + quote(entry) + " \"$@\"\n"
	if err := os.WriteFile(cold, []byte(body), 0700); err != nil {
		t.Fatal(err)
	}
	plan.IdentityCommand[0] = cold
	for index := range plan.Gates {
		plan.Gates[index].Argv[0] = cold
	}
	self, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	plan.HealthCommand[0] = self
	if err := verificationValidate(plan); err != nil {
		t.Fatal(err)
	}
	if code := verificationRun(plan, output); code != 0 {
		t.Fatalf("prepared native status code %d", code)
	}
	for _, path := range []string{marker, plan.HealthCommand[2]} {
		if _, err := os.Stat(path); !os.IsNotExist(err) {
			t.Fatalf("inspection entered cold launcher or created state: %s, %v", path, err)
		}
	}
	// Fail control: routing health through the cold launcher still fails closed.
	plan.HealthCommand[0] = cold
	failed := physicalTempDir(t)
	if code := verificationRun(plan, failed); code != 2 {
		t.Fatalf("cold launcher control code %d", code)
	}
	if _, err := os.Stat(marker); err != nil {
		t.Fatal("cold launcher control was not exercised", err)
	}
	report := verificationReadReport(t, failed)
	if report.Reason != "unsafe_initial_service_state" || report.Gates[0].Status != "unrun" {
		t.Fatalf("report %+v", report)
	}
}

func TestVerificationPreparedStatusRejectsBusyScopeWithoutReaping(t *testing.T) {
	plan, _ := verificationFixture(t, "first")
	state := plan.HealthCommand[2]
	if err := private(state); err != nil {
		t.Fatal(err)
	}
	if err := atomic(filepath.Join(state, "plan.json"), Plan{State: state, Services: map[string]Service{}}); err != nil {
		t.Fatal(err)
	}
	lease := filepath.Join(state, "abandoned.lease")
	if err := atomic(lease, Lease{Services: []string{"abandoned"}}); err != nil {
		t.Fatal(err)
	}
	before, _ := os.ReadFile(lease)
	gate, err := locked(filepath.Join(state, "gate"), false)
	if err != nil {
		t.Fatal(err)
	}
	defer gate.Close()
	plan.HealthCommand[0], err = os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	if _, err := verificationCheckHealth(plan, nil); err == nil {
		t.Fatal("busy incomplete scope accepted")
	}
	after, err := os.ReadFile(lease)
	if err != nil || string(after) != string(before) {
		t.Fatal("inspection changed a lease", err)
	}
}

func TestVerificationRejectsHealthFromDifferentScope(t *testing.T) {
	plan, _ := verificationFixture(t, "first")
	plan.HealthCommand[2] = filepath.Join(plan.Root, "other-scope")
	if _, err := verificationCheckHealth(plan, nil); err == nil {
		t.Fatal("different healthy scope accepted")
	}
}

func TestVerificationPreparedStatusPreservesSharedRecoveryAndLeases(t *testing.T) {
	plan, _ := verificationFixture(t, "first")
	state := plan.HealthCommand[2]
	resource := filepath.Join(plan.Root, "shared-resource")
	for _, path := range []string{state, resource} {
		if err := private(path); err != nil {
			t.Fatal(err)
		}
	}
	shared := Plan{State: resource, Services: map[string]Service{}}
	if err := atomic(filepath.Join(resource, "plan.json"), shared); err != nil {
		t.Fatal(err)
	}
	root := Plan{State: state, Services: map[string]Service{}, Resources: []Plan{shared}}
	if err := atomic(filepath.Join(state, "plan.json"), root); err != nil {
		t.Fatal(err)
	}
	self, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	plan.HealthCommand[0] = self
	baseline, err := verificationCheckHealth(plan, nil)
	if err != nil || len(baseline) != 2 {
		t.Fatal("shared native baseline unavailable", baseline, err)
	}
	lease := filepath.Join(resource, "existing.lease")
	if err := atomic(lease, Lease{Persistent: true, Services: []string{"worker"}}); err != nil {
		t.Fatal(err)
	}
	before, _ := os.ReadFile(lease)
	// A persistent client whose controller is absent requires recovery. Health
	// must inspect the saved shared scope, fail closed and preserve its lease.
	if _, err := verificationCheckHealth(plan, baseline); err == nil {
		t.Fatal("changed shared ownership/recovery accepted")
	}
	after, err := os.ReadFile(lease)
	if err != nil || string(before) != string(after) {
		t.Fatal("shared inspection changed existing lease", err)
	}
}
