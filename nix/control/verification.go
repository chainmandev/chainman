package main

// Verification coordinates existing verified task entries. It never owns a
// service directly, changes a gate, or converts a failed gate into acceptance.
import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"reflect"
	"syscall"
	"time"
)

type VerificationIdentity struct {
	Commit      string `json:"commit"`
	Fingerprint string `json:"source_fingerprint"`
	Pin         string `json:"runtime_pin"`
}
type VerificationGate struct {
	Name         string   `json:"name"`
	Argv         []string `json:"argv"`
	Dependencies []string `json:"depends_on"`
}
type VerificationPlan struct {
	Schema          int                  `json:"schema"`
	Root            string               `json:"root"`
	IdentityCommand []string             `json:"identity_command"`
	IdentityRoot    string               `json:"identity_root,omitempty"`
	IdentityMounts  string               `json:"identity_mounts,omitempty"`
	HealthCommand   []string             `json:"health_command"`
	Identity        VerificationIdentity `json:"identity"`
	Gates           []VerificationGate   `json:"gates"`
}
type VerificationResult struct {
	Name    string  `json:"name"`
	Status  string  `json:"status"`
	Code    *int    `json:"exit_code"`
	Seconds float64 `json:"duration_seconds"`
	Log     string  `json:"log,omitempty"`
	Reason  string  `json:"reason,omitempty"`
}
type VerificationReport struct {
	Schema   int                  `json:"schema"`
	Root     string               `json:"root"`
	Identity VerificationIdentity `json:"identity"`
	Started  time.Time            `json:"started_at"`
	Ended    *time.Time           `json:"ended_at"`
	Code     int                  `json:"exit_code"`
	Reason   string               `json:"reason,omitempty"`
	Gates    []VerificationResult `json:"gates"`
}

func verificationValidate(p VerificationPlan) error {
	if p.Schema != 1 || !filepath.IsAbs(p.Root) || filepath.Clean(p.Root) != p.Root || len(p.Gates) == 0 ||
		(len(p.IdentityCommand) != 2 && len(p.IdentityCommand) != 3) || len(p.HealthCommand) != 2 ||
		p.IdentityCommand[1] != "_verification-identity" || p.HealthCommand[1] != "services-status" ||
		p.Identity.Commit == "" || len(p.Identity.Fingerprint) != 64 || len(p.Identity.Pin) != 40 {
		return fmt.Errorf("invalid verification plan")
	}
	if len(p.IdentityCommand) == 3 && (p.IdentityCommand[2] != p.Root || !filepath.IsAbs(p.IdentityRoot) || !filepath.IsAbs(p.IdentityMounts)) {
		return fmt.Errorf("invalid verification identity boundary")
	}
	seen := map[string]bool{}
	for _, gate := range p.Gates {
		if !validName.MatchString(gate.Name) || seen[gate.Name] || len(gate.Argv) != 3 || gate.Argv[2] != gate.Name ||
			(gate.Argv[1] != "run" && !(gate.Argv[1] == "_update-verify" && gate.Name == "legacy")) {
			return fmt.Errorf("invalid verification gate")
		}
		for _, required := range gate.Dependencies {
			if !seen[required] {
				return fmt.Errorf("verification prerequisite must precede its gate")
			}
		}
		seen[gate.Name] = true
	}
	return nil
}

// Unknown status fields are forward compatible, but missing safety observations
// are not. Existing users' leases are preserved rather than stopped or borrowed.
type verificationHealth struct {
	State     string               `json:"state"`
	Complete  *bool                `json:"complete"`
	Busy      *bool                `json:"busy"`
	Recovery  *bool                `json:"recovery_required"`
	Running   *bool                `json:"running"`
	Leases    map[string]bool      `json:"leases"`
	Errors    []string             `json:"inspection_errors"`
	Resources []verificationHealth `json:"resources"`
}

func verificationScopes(h verificationHealth, out map[string]verificationHealth) error {
	if h.Complete == nil || !*h.Complete || h.Busy == nil || *h.Busy || h.Recovery == nil || *h.Recovery ||
		h.Running == nil || h.Leases == nil || len(h.Errors) != 0 || h.State == "" {
		return fmt.Errorf("service cleanup cannot be established safely")
	}
	if _, exists := out[h.State]; exists {
		return fmt.Errorf("duplicate service scope")
	}
	out[h.State] = h
	for _, resource := range h.Resources {
		if err := verificationScopes(resource, out); err != nil {
			return err
		}
	}
	return nil
}
func verificationCommand(p VerificationPlan, argv []string) *exec.Cmd {
	cmd := exec.Command(argv[0], argv[1:]...)
	cmd.Dir = p.Root
	cmd.Env = append(os.Environ(), "CHAINMAN_PROJECT_ROOT="+p.Root)
	cmd.Stdin = os.Stdin
	return cmd
}
func verificationQuery(p VerificationPlan, argv []string, output any) error {
	cmd := verificationCommand(p, argv)
	if reflect.DeepEqual(argv, p.IdentityCommand) && p.IdentityRoot != "" {
		cmd.Dir = p.IdentityRoot
		cmd.Env = append(cmd.Env, "CHAINMAN_PROJECT_ROOT="+p.IdentityRoot, "CHAINMAN_CONTAINER_OPTIONS_FILE="+p.IdentityMounts, "CHAINMAN_WORKSPACE_TRANSACTION_ROOT=")
	}
	var body bytes.Buffer
	cmd.Stdout, cmd.Stderr = &body, os.Stderr
	if err := hookRunBounded(cmd, 0, 60*time.Second); err != nil {
		return err
	}
	return json.Unmarshal(body.Bytes(), output)
}
func verificationCheckIdentity(p VerificationPlan) error {
	var identity VerificationIdentity
	if err := verificationQuery(p, p.IdentityCommand, &identity); err != nil {
		return err
	}
	if identity != p.Identity {
		return fmt.Errorf("source or runtime identity changed during verification")
	}
	return nil
}
func verificationCheckHealth(p VerificationPlan, baseline map[string]verificationHealth) (map[string]verificationHealth, error) {
	var health verificationHealth
	if err := verificationQuery(p, p.HealthCommand, &health); err != nil {
		return nil, err
	}
	scopes := map[string]verificationHealth{}
	if err := verificationScopes(health, scopes); err != nil {
		return nil, err
	}
	if baseline != nil {
		for name, scope := range scopes {
			previous, exists := baseline[name]
			if exists {
				if !reflect.DeepEqual(scope.Leases, previous.Leases) || *scope.Running != *previous.Running {
					return nil, fmt.Errorf("service ownership changed during verification")
				}
			} else if *scope.Running || len(scope.Leases) != 0 {
				return nil, fmt.Errorf("verification left a live service scope")
			}
		}
		for name, previous := range baseline {
			if _, exists := scopes[name]; !exists && (*previous.Running || len(previous.Leases) != 0) {
				return nil, fmt.Errorf("existing service ownership became unavailable")
			}
		}
	}
	return scopes, nil
}
func verificationInterrupted(signals <-chan os.Signal) int {
	select {
	case sig := <-signals:
		return 128 + int(sig.(syscall.Signal))
	default:
		return 0
	}
}

func verificationRun(p VerificationPlan, directory string) (result int) {
	report := VerificationReport{Schema: 1, Root: p.Root, Identity: p.Identity, Started: time.Now().UTC(), Code: 1}
	for _, gate := range p.Gates {
		report.Gates = append(report.Gates, VerificationResult{Name: gate.Name, Status: "unrun"})
	}
	path := filepath.Join(directory, "report.json")
	signals := make(chan os.Signal, 8)
	signal.Notify(signals, syscall.SIGINT, syscall.SIGTERM, syscall.SIGHUP)
	defer signal.Stop(signals)
	defer func() {
		if code := verificationInterrupted(signals); code != 0 {
			result = code
		}
		now := time.Now().UTC()
		report.Ended, report.Code = &now, result
		if err := atomic(path, report); err != nil {
			fmt.Fprintln(os.Stderr, "Verification report:", err)
			if result == 0 {
				result = 1
			}
		}
		fmt.Fprintln(os.Stderr, "\nVerification summary:")
		for _, gate := range report.Gates {
			fmt.Fprintf(os.Stderr, "  %-10s %s (%.1fs) %s\n", gate.Status, gate.Name, gate.Seconds, gate.Reason)
		}
		fmt.Fprintln(os.Stderr, "Verification report:", path)
	}()
	result = 1
	if err := atomic(path, report); err != nil {
		return result
	}
	baseline, err := verificationCheckHealth(p, nil)
	if err != nil {
		report.Reason = "unsafe_initial_service_state"
		fmt.Fprintln(os.Stderr, err)
		return result
	}
	if err = verificationCheckIdentity(p); err != nil {
		report.Reason = "source_identity_unavailable_or_changed"
		fmt.Fprintln(os.Stderr, err)
		return result
	}
	statuses := map[string]string{}
	result = 0
	for index, gate := range p.Gates {
		if code := verificationInterrupted(signals); code != 0 {
			report.Reason = "interrupted"
			return code
		}
		row := &report.Gates[index]
		blocked := false
		for _, required := range gate.Dependencies {
			if statuses[required] != "passed" {
				blocked = true
			}
		}
		if blocked {
			row.Status, row.Reason = "blocked", "failed_prerequisite"
			statuses[gate.Name], result = row.Status, 1
			if err = atomic(path, report); err != nil {
				return 1
			}
			continue
		}
		row.Log = filepath.Join(directory, fmt.Sprintf("%02d-%s.log", index+1, gate.Name))
		log, openError := os.OpenFile(row.Log, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0600)
		if openError != nil {
			report.Reason = "log_creation_failed"
			return 1
		}
		row.Status = "running"
		if err = atomic(path, report); err != nil {
			log.Close()
			return 1
		}
		fmt.Fprintf(os.Stderr, "\nVerification gate %d/%d: %s\n", index+1, len(p.Gates), gate.Name)
		started := time.Now()
		cmd := verificationCommand(p, gate.Argv)
		cmd.Stdout, cmd.Stderr = io.MultiWriter(os.Stdout, log), io.MultiWriter(os.Stderr, log)
		code := exitCode(hookRunWithShutdown(cmd, 0, 0, 300*time.Second))
		row.Seconds, row.Code = time.Since(started).Seconds(), &code
		row.Status = "passed"
		if code != 0 {
			row.Status, result = "failed", 1
		}
		if closeError := log.Close(); closeError != nil {
			report.Reason = "log_write_failed"
			return 1
		}
		if interrupted := verificationInterrupted(signals); interrupted != 0 {
			row.Status, report.Reason = "interrupted", "interrupted"
			return interrupted
		}
		if code == 129 || code == 130 || code == 143 {
			row.Status, report.Reason = "interrupted", "interrupted"
			return code
		}
		statuses[gate.Name] = row.Status
		if err = atomic(path, report); err != nil {
			return 1
		}
		if err = verificationCheckIdentity(p); err != nil {
			report.Reason = "source_identity_unavailable_or_changed"
			fmt.Fprintln(os.Stderr, err)
			return 1
		}
		if _, err = verificationCheckHealth(p, baseline); err != nil {
			report.Reason = "unsafe_service_cleanup"
			fmt.Fprintln(os.Stderr, err)
			return 1
		}
	}
	return result
}
func verificationAction(args []string) int {
	if len(args) != 2 {
		return 2
	}
	var plan VerificationPlan
	if err := readJSON(args[0], &plan); err != nil {
		return exitCode(err)
	}
	if err := verificationValidate(plan); err != nil {
		return exitCode(err)
	}
	if err := existingPrivate(args[1]); err != nil {
		return exitCode(err)
	}
	relative, err := filepath.Rel(plan.Root, args[1])
	if err != nil || relative == "." || (relative != ".." && !filepath.IsAbs(relative) && !bytes.HasPrefix([]byte(relative), []byte("../"))) {
		return exitCode(fmt.Errorf("verification reports must be outside the project"))
	}
	directory, err := os.MkdirTemp(args[1], "verification-")
	if err != nil {
		return exitCode(err)
	}
	return verificationRun(plan, directory)
}
