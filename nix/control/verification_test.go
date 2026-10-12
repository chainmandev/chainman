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
	health := `{"state":"fixture","complete":true,"busy":false,"recovery_required":false,"running":false,"leases":{},"inspection_errors":[],"resources":[]}`
	script := "#!/bin/sh\nset -eu\ncase \"$1\" in\n" +
		"_verification-identity) if [ -e changed ]; then printf '{}'; else printf '%s' " + quote(string(body)) + "; fi ;;\n" +
		"services-status) if [ -e unsafe ]; then printf '{}'; else printf '%s' " + quote(health) + "; fi ;;\n" +
		"run) printf '%s\\n' \"$2\" >> events; printf 'output %s\\n' \"$2\"; case \"$2\" in\n" +
		"fail) exit 7 ;; unsafe) touch unsafe; exit 8 ;; mutate) touch changed ;;\n" +
		"slow) trap 'exit 0' TERM INT HUP; touch ready; while :; do sleep 1; done ;;\n" +
		"esac ;; *) exit 2 ;; esac\n"
	if err := os.WriteFile(entry, []byte(script), 0700); err != nil {
		t.Fatal(err)
	}
	plan := VerificationPlan{Schema: 1, Root: root, Identity: identity,
		IdentityCommand: []string{entry, "_verification-identity"}, HealthCommand: []string{entry, "services-status"}}
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
			if code := verificationRun(plan, output); code != 1 {
				t.Fatalf("code %d", code)
			}
			report := verificationReadReport(t, output)
			if report.Reason == "" || report.Gates[1].Status != "unrun" {
				t.Fatalf("report %+v", report)
			}
		})
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
