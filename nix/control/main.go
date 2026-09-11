// Chainman's host-side ownership adapter. Process Compose owns supervision,
// readiness and restart policy; this program owns leases and crash recovery.
package main

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"syscall"
	"time"
)

type Command struct {
	Argv        []string          `json:"argv"`
	Directory   string            `json:"directory"`
	Environment map[string]string `json:"environment,omitempty"`
}
type Probe struct {
	Command  Command `json:"command"`
	Period   int     `json:"period_seconds"`
	Timeout  int     `json:"timeout_seconds"`
	Failures int     `json:"failure_threshold"`
}
type Service struct {
	Command      Command  `json:"command"`
	Dependencies []string `json:"depends_on"`
	Readiness    *Probe   `json:"readiness,omitempty"`
	Restart      string   `json:"restart"`
	Shutdown     int      `json:"shutdown_seconds"`
	// Container cleanup addresses an immutable engine ID after checking its label.
	Container     *Container `json:"container,omitempty"`
	Watch         *Watch     `json:"watch,omitempty"`
	Timeout       int        `json:"timeout_seconds,omitempty"`
	ForwardLeases bool       `json:"forward_leases,omitempty"`
}
type Container struct {
	Engine         string `json:"engine"`
	Name           string `json:"name"`
	Token          string `json:"token"`
	EngineIdentity string `json:"engine_identity,omitempty"`
}
type Plan struct {
	Schema        int                `json:"schema"`
	Root          string             `json:"root"`
	State         string             `json:"state"`
	Backend       string             `json:"backend"`
	Watcher       string             `json:"watcher,omitempty"`
	Licenses      map[string]string  `json:"licenses,omitempty"`
	Fingerprint   string             `json:"fingerprint"`
	Services      map[string]Service `json:"services"`
	Requested     []string           `json:"requested"`
	Task          Command            `json:"task"`
	Prepare       *Command           `json:"prepare,omitempty"`
	TaskContainer *Container         `json:"task_container,omitempty"`
}
type Identity struct {
	PID   int    `json:"pid"`
	Birth string `json:"birth"`
}
type Owner struct {
	Identity  Identity   `json:"identity"`
	Container *Container `json:"container,omitempty"`
}
type Lease struct {
	Services   []string   `json:"services"`
	Persistent bool       `json:"persistent"`
	Container  *Container `json:"container,omitempty"`
	Task       *Identity  `json:"task,omitempty"`
}
type Process struct {
	Name    string `json:"name"`
	Status  string `json:"status"`
	Ready   string `json:"is_ready"`
	Running bool   `json:"is_running"`
}

var validName = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_-]*$`)

func readJSON(path string, v any) error {
	f, err := os.OpenFile(path, os.O_RDONLY|syscall.O_NOFOLLOW, 0)
	if err != nil {
		return err
	}
	defer f.Close()
	st, err := f.Stat()
	if err != nil {
		return err
	}
	if !st.Mode().IsRegular() || st.Size() > 4<<20 {
		return fmt.Errorf("invalid state file: %s", path)
	}
	dec := json.NewDecoder(io.LimitReader(f, 4<<20))
	dec.DisallowUnknownFields()
	if err = dec.Decode(v); err != nil {
		return err
	}
	if dec.Decode(new(any)) != io.EOF {
		return fmt.Errorf("trailing state data")
	}
	return nil
}
func atomic(path string, v any) error {
	b, err := json.Marshal(v)
	if err != nil {
		return err
	}
	f, err := os.CreateTemp(filepath.Dir(path), ".write-")
	if err != nil {
		return err
	}
	defer os.Remove(f.Name())
	defer f.Close()
	if _, err = f.Write(b); err != nil {
		return err
	}
	if err = f.Sync(); err != nil {
		return err
	}
	if err = f.Close(); err != nil {
		return err
	}
	return os.Rename(f.Name(), path)
}
func private(path string) error {
	if !filepath.IsAbs(path) || filepath.Clean(path) != path {
		return fmt.Errorf("state paths must be absolute and normalized")
	}
	// Reject links in existing ancestors before mkdir; private parents prevent other
	// users from replacing state after validation.
	for p := path; p != "/"; p = filepath.Dir(p) {
		st, err := os.Lstat(p)
		if os.IsNotExist(err) {
			continue
		}
		if err != nil {
			return err
		}
		if st.Mode()&os.ModeSymlink != 0 || !st.IsDir() {
			return fmt.Errorf("state path contains indirection: %s", p)
		}
	}
	if err := os.MkdirAll(path, 0700); err != nil {
		return err
	}
	st, err := os.Stat(path)
	if err != nil {
		return err
	}
	uid := st.Sys().(*syscall.Stat_t).Uid
	if int(uid) != os.Geteuid() || st.Mode().Perm()&0077 != 0 {
		return fmt.Errorf("state must be owned by this user and mode 0700: %s", path)
	}
	return nil
}
func locked(path string, nonblocking bool) (*os.File, error) {
	f, err := os.OpenFile(path, os.O_CREATE|os.O_RDWR|syscall.O_NOFOLLOW, 0600)
	if err != nil {
		return nil, err
	}
	flag := syscall.LOCK_EX
	if nonblocking {
		flag |= syscall.LOCK_NB
	}
	if err = syscall.Flock(int(f.Fd()), flag); err != nil {
		f.Close()
		return nil, err
	}
	return f, nil
}
func identify(pid int) (Identity, error) { b, e := birth(pid); return Identity{pid, b}, e }
func (i Identity) alive() bool {
	if i.PID <= 1 || i.Birth == "" {
		return false
	}
	b, e := birth(i.PID)
	return e == nil && b == i.Birth
}
func token() string {
	b := make([]byte, 16)
	if _, e := rand.Read(b); e != nil {
		panic(e)
	}
	return hex.EncodeToString(b)
}
func quote(s string) string { return "'" + strings.ReplaceAll(s, "'", "'\"'\"'") + "'" }
func shell(args ...string) string {
	a := make([]string, len(args))
	for i, s := range args {
		a[i] = quote(s)
	}
	return "exec " + strings.Join(a, " ")
}
func child(c Command) (*exec.Cmd, error) {
	if len(c.Argv) == 0 {
		return nil, fmt.Errorf("empty command")
	}
	for _, arg := range c.Argv {
		if strings.ContainsRune(arg, 0) {
			return nil, fmt.Errorf("NUL in command")
		}
	}
	cmd := exec.Command(c.Argv[0], c.Argv[1:]...)
	cmd.Dir = c.Directory
	cmd.Env = os.Environ()
	// Process Compose does not forward arbitrary descriptor leases. Never advertise
	// the parent's closed descriptors to a new Chainman execution.
	filtered := cmd.Env[:0]
	for _, v := range cmd.Env {
		k, _, _ := strings.Cut(v, "=")
		stale := map[string]bool{"TOOLCHAIN_LOCK_FD": true, "TOOLCHAIN_GATE_FD": true, "TOOLCHAIN_COMPAT_FD": true, "TOOLCHAIN_OPERATION_ID": true, "TOOLCHAIN_ANCESTOR_FDS": true, "CHAINMAN_COMPILER_OWNER": true}
		if !stale[k] && !strings.HasPrefix(k, "CHAINMAN_OPERATION_") {
			filtered = append(filtered, v)
		}
	}
	cmd.Env = filtered
	for k, v := range c.Environment {
		if strings.ContainsAny(k, "=\x00") || strings.ContainsRune(v, 0) {
			return nil, fmt.Errorf("invalid environment")
		}
		cmd.Env = append(cmd.Env, k+"="+v)
	}
	cmd.Stdin = os.Stdin
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr
	return cmd, nil
}
func backend(p Plan, args ...string) ([]byte, error) {
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	socket, e := socketPath(p)
	if e != nil {
		return nil, e
	}
	base := []string{"--use-uds", "--unix-socket", socket, "--log-file", os.DevNull}
	cmd := exec.CommandContext(ctx, p.Backend, append(base, args...)...)
	out, err := cmd.Output()
	if err != nil {
		var failure *exec.ExitError
		detail := ""
		if errors.As(err, &failure) {
			detail = strings.TrimSpace(string(failure.Stderr))
		}
		return out, fmt.Errorf("Process Compose %s: %w %s", strings.Join(args, " "), err, detail)
	}
	return out, err
}
func socketPath(p Plan) (string, error) {
	base, e := filepath.EvalSymlinks("/tmp")
	if e != nil {
		return "", e
	}
	directory := filepath.Join(base, fmt.Sprintf("chainman-control-%d", os.Geteuid()))
	if e = private(directory); e != nil {
		return "", e
	}
	if directory == p.Root || strings.HasPrefix(directory, p.Root+"/") {
		return "", fmt.Errorf("controller socket cannot be inside project mounts")
	}
	key := sha256.Sum256([]byte(p.State))
	return filepath.Join(directory, hex.EncodeToString(key[:12])+".sock"), nil
}
func states(p Plan) ([]Process, error) {
	b, e := backend(p, "process", "list", "-o", "json")
	if e != nil {
		return nil, e
	}
	var r []Process
	e = json.Unmarshal(b, &r)
	return r, e
}
func controller(p Plan) Identity {
	var i Identity
	_ = readJSON(filepath.Join(p.State, "controller.json"), &i)
	return i
}
func validate(p Plan) error {
	if p.Schema != 1 || !filepath.IsAbs(p.Root) || !filepath.IsAbs(p.Backend) || p.Fingerprint == "" {
		return fmt.Errorf("invalid controller plan")
	}
	if p.State == p.Root || strings.HasPrefix(p.State, p.Root+string(os.PathSeparator)) {
		return fmt.Errorf("host control state must be outside project mounts")
	}
	if len(p.Services) > 128 {
		return fmt.Errorf("too many services")
	}
	_, e := ordered(p, p.Requested)
	if e != nil {
		return e
	}
	for name, s := range p.Services {
		if !validName.MatchString(name) {
			return fmt.Errorf("invalid service name")
		}
		if _, e := child(s.Command); e != nil {
			return e
		}
		if s.Shutdown < 1 || s.Shutdown > 300 {
			return fmt.Errorf("invalid shutdown timeout")
		}
		if s.Restart != "no" && s.Restart != "always" && s.Restart != "on_failure" {
			return fmt.Errorf("invalid restart policy")
		}
		if s.Readiness != nil {
			r := s.Readiness
			if _, e := child(r.Command); e != nil {
				return e
			}
			if r.Period < 1 || r.Timeout < 1 || r.Failures < 1 || r.Period*r.Failures+r.Timeout > 600 {
				return fmt.Errorf("invalid readiness bounds")
			}
		}
		if s.Container != nil {
			c := s.Container
			if !filepath.IsAbs(c.Engine) || !validName.MatchString(c.Name) || len(c.Token) != 32 {
				return fmt.Errorf("invalid container ownership")
			}
		}
	}
	return nil
}
func ordered(p Plan, names []string) ([]string, error) {
	result := []string{}
	seen := map[string]int{}
	var visit func(string) error
	visit = func(n string) error {
		if seen[n] == 1 {
			return fmt.Errorf("service dependency cycle: %s", n)
		}
		if seen[n] == 2 {
			return nil
		}
		s, ok := p.Services[n]
		if !ok {
			return fmt.Errorf("unknown service: %s", n)
		}
		seen[n] = 1
		for _, d := range s.Dependencies {
			if e := visit(d); e != nil {
				return e
			}
		}
		seen[n] = 2
		result = append(result, n)
		return nil
	}
	for _, n := range names {
		if e := visit(n); e != nil {
			return nil, e
		}
	}
	return result, nil
}
func configuration(p Plan, self string) error {
	processes := map[string]any{}
	for n, s := range p.Services {
		if s.Watch != nil {
			if e := watchStopping(p, n, false); e != nil {
				return e
			}
			// Invalidate readiness before Process Compose can launch any probes.
			// Removing it inside the watcher leaves a stale-readiness startup race.
			if e := os.Remove(filepath.Join(p.State, n+".built.json")); e != nil && !os.IsNotExist(e) {
				return e
			}
		}
		spec := filepath.Join(p.State, n+".command.json")
		if e := atomic(spec, s); e != nil {
			return e
		}
		d := map[string]any{"command": "exec \"$CHAINMAN_CONTROL_EXECUTABLE\" exec \"$CHAINMAN_CONTROL_STATE\" " + quote(n), "is_template_disabled": true, "availability": map[string]any{"restart": s.Restart}, "shutdown": map[string]any{"timeout_seconds": s.Shutdown + 2}}
		deps := map[string]any{}
		for _, name := range s.Dependencies {
			condition := "process_started"
			if p.Services[name].Readiness != nil {
				condition = "process_healthy"
			}
			deps[name] = map[string]any{"condition": condition}
		}
		if len(deps) > 0 {
			d["depends_on"] = deps
		}
		if s.Readiness != nil {
			r := s.Readiness
			d["readiness_probe"] = map[string]any{"exec": map[string]string{"command": "exec \"$CHAINMAN_CONTROL_EXECUTABLE\" probe \"$CHAINMAN_CONTROL_STATE\" " + quote(n)}, "period_seconds": r.Period, "timeout_seconds": r.Timeout, "failure_threshold": r.Failures}
		}
		processes[n] = d
	}
	return atomic(filepath.Join(p.State, "compose.json"), map[string]any{
		"version": "0.5", "processes": processes, "log_location": "services.log", "log_length": 500,
		"log_configuration": map[string]any{"rotation": map[string]any{"max_size_mb": 10, "max_backups": 3, "max_age_days": 7}},
	})
}
func start(p Plan, self string) error {
	if e := configuration(p, self); e != nil {
		return e
	}
	if e := atomic(filepath.Join(p.State, "plan.json"), p); e != nil {
		return e
	}
	log, e := os.OpenFile(os.DevNull, os.O_WRONLY, 0)
	if e != nil {
		return e
	}
	defer log.Close()
	socket, e := socketPath(p)
	if e != nil {
		return e
	}
	args := []string{"--use-uds", "--unix-socket", socket, "--log-file", os.DevNull, "--ordered-shutdown", "up", "--config", filepath.Join(p.State, "compose.json"), "--disable-dotenv", "--tui=false", "--keep-project"}
	cmd := exec.Command(p.Backend, append(args, p.Requested...)...)
	cmd.Dir = p.State
	cmd.Env = append(os.Environ(), "CHAINMAN_CONTROL_EXECUTABLE="+self, "CHAINMAN_CONTROL_STATE="+p.State)
	cmd.Stdout = log
	cmd.Stderr = log
	cmd.SysProcAttr = &syscall.SysProcAttr{Setsid: true}
	if e = cmd.Start(); e != nil {
		return e
	}
	id, e := identify(cmd.Process.Pid)
	if e != nil {
		_ = cmd.Process.Kill()
		_ = cmd.Wait()
		return e
	}
	if e = atomic(filepath.Join(p.State, "controller.json"), id); e != nil {
		_ = cmd.Process.Kill()
		_ = cmd.Wait()
		return e
	}
	go cmd.Wait()
	deadline := time.Now().Add(15 * time.Second)
	for time.Now().Before(deadline) {
		if _, e = states(p); e == nil {
			return nil
		}
		if !id.alive() {
			break
		}
		time.Sleep(50 * time.Millisecond)
	}
	return fmt.Errorf("Process Compose failed to start (%v); service output: %s", e, filepath.Join(p.State, "services.log"))
}
func ready(p Plan, names []string) error {
	// Probe execution, thresholds and dependency state are owned by Process Compose.
	// This deadline only bounds a failed/lost controller, not a second probe loop.
	seconds := 15
	for _, n := range names {
		if r := p.Services[n].Readiness; r != nil {
			seconds += r.Period*r.Failures + r.Timeout
		}
	}
	deadline := time.Now().Add(time.Duration(seconds) * time.Second)
	for time.Now().Before(deadline) {
		unlock, e := watchGates(p, names)
		if e != nil {
			return e
		}
		ps, e := states(p)
		unlock()
		if e != nil {
			return fmt.Errorf("service controller lost: %w", e)
		}
		found := map[string]Process{}
		for _, s := range ps {
			found[s.Name] = s
		}
		all := true
		for _, n := range names {
			s, ok := found[n]
			if !ok {
				return fmt.Errorf("backend omitted service %s", n)
			}
			if s.Status == "Completed" || s.Status == "Error" || s.Status == "Skipped" {
				return fmt.Errorf("service %s failed: %s", n, s.Status)
			}
			if !s.Running || (p.Services[n].Readiness != nil && s.Ready != "Ready") {
				all = false
			}
		}
		if all {
			return nil
		}
		time.Sleep(100 * time.Millisecond)
	}
	return fmt.Errorf("service readiness deadline exceeded")
}
func active(p Plan) (map[string]bool, error) {
	result := map[string]bool{}
	entries, e := os.ReadDir(p.State)
	if e != nil {
		return nil, e
	}
	for _, entry := range entries {
		if !strings.HasSuffix(entry.Name(), ".lease") {
			continue
		}
		path := filepath.Join(p.State, entry.Name())
		var l Lease
		if e = readJSON(path, &l); e != nil {
			return nil, e
		}
		f, err := locked(path, true)
		if err == nil {
			f.Close()
			if !l.Persistent {
				present := l.Task != nil && l.Task.alive()
				if !present && l.Container != nil {
					info, err := inspectContainer(l.Container)
					if err != nil {
						return nil, err
					}
					present = info != nil && info.Running
				}
				if !present {
					if e = os.Remove(path); e != nil {
						return nil, e
					}
					continue
				}
			}
		} else if !errors.Is(err, syscall.EWOULDBLOCK) {
			return nil, err
		}
		for _, n := range l.Services {
			result[n] = true
		}
	}
	return result, nil
}

type ContainerState struct {
	ID      string
	Running bool
}

func inspectContainer(c *Container) (*ContainerState, error) {
	if e := checkEngine(c); e != nil {
		return nil, e
	}
	// The random owner label and immutable ID prevent deletion of a replacement
	// container that happens to reuse a friendly name.
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	out, e := exec.CommandContext(ctx, c.Engine, "container", "inspect", c.Name).Output()
	if e != nil {
		// An engine outage is not evidence of absence. A separate list must succeed.
		list, err := exec.CommandContext(ctx, c.Engine, "container", "ls", "-aq", "--filter", "name=^/"+c.Name+"$").Output()
		if err != nil {
			return nil, fmt.Errorf("cannot inspect owned container: %w", err)
		}
		if len(strings.TrimSpace(string(list))) == 0 {
			return nil, nil
		}
		return nil, e
	}
	var entries []struct {
		ID    string `json:"Id"`
		State struct {
			Running *bool `json:"Running"`
		} `json:"State"`
		Config struct {
			Labels map[string]string `json:"Labels"`
		} `json:"Config"`
	}
	if e = json.Unmarshal(out, &entries); e != nil {
		return nil, e
	}
	if len(entries) != 1 || entries[0].Config.Labels["dev.chainman.owner"] != c.Token {
		return nil, fmt.Errorf("container ownership changed: %s", c.Name)
	}
	if entries[0].State.Running == nil {
		return nil, fmt.Errorf("engine omitted container liveness")
	}
	id := entries[0].ID
	if id == "" {
		return nil, fmt.Errorf("missing immutable container ID")
	}
	return &ContainerState{id, *entries[0].State.Running}, nil
}
func engineIdentity(engine string) (string, error) {
	format := "{{.ID}}"
	if filepath.Base(engine) == "podman" {
		format = "{{.Host.Hostname}} {{.Store.GraphRoot}} {{.Host.Security.Rootless}}"
	} else if filepath.Base(engine) != "docker" {
		return "", fmt.Errorf("unsupported host engine")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	data, e := exec.CommandContext(ctx, engine, "info", "--format", format).Output()
	if e != nil {
		return "", e
	}
	value := strings.TrimSpace(string(data))
	if value == "" || strings.Contains(value, "<no value>") {
		return "", fmt.Errorf("host engine did not supply an identity")
	}
	return value, nil
}
func checkEngine(c *Container) error {
	if c == nil || c.EngineIdentity == "" {
		return nil
	}
	current, e := engineIdentity(c.Engine)
	if e != nil {
		return e
	}
	if current != c.EngineIdentity {
		return fmt.Errorf("selected engine context differs from the owner of %s; restore that context before recovery", c.Name)
	}
	return nil
}
func bindEngines(p *Plan) error {
	identities := map[string]string{}
	containers := []*Container{p.TaskContainer}
	for _, s := range p.Services {
		containers = append(containers, s.Container)
		if s.Watch != nil {
			containers = append(containers, s.Watch.Container)
		}
	}
	for _, c := range containers {
		if c == nil {
			continue
		}
		identity, ok := identities[c.Engine]
		if !ok {
			var e error
			identity, e = engineIdentity(c.Engine)
			if e != nil {
				return e
			}
			identities[c.Engine] = identity
		}
		c.EngineIdentity = identity
	}
	if len(identities) > 0 {
		data, e := json.Marshal(identities)
		if e != nil {
			return e
		}
		hash := sha256.Sum256(data)
		p.Fingerprint += "/" + hex.EncodeToString(hash[:])
	}
	return nil
}
func stopContainer(c *Container, seconds int) error {
	if c == nil {
		return nil
	}
	info, e := inspectContainer(c)
	if e != nil {
		return e
	}
	if info == nil {
		return nil
	}
	id := info.ID
	ctx, cancel := context.WithTimeout(context.Background(), time.Duration(seconds+15)*time.Second)
	defer cancel()
	cmd := exec.CommandContext(ctx, c.Engine, "container", "stop", "--time", fmt.Sprint(seconds), id)
	if b, e := cmd.CombinedOutput(); e != nil {
		return fmt.Errorf("owned container stop: %w: %s", e, b)
	}
	return nil
}
func stopOwner(p Plan, n string) error {
	path := filepath.Join(p.State, n+".owner.json")
	var o Owner
	e := readJSON(path, &o)
	if os.IsNotExist(e) {
		return nil
	}
	if e != nil {
		return e
	}
	s := p.Services[n]
	if o.Identity.alive() {
		if e = syscall.Kill(-o.Identity.PID, syscall.SIGTERM); e != nil && e != syscall.ESRCH {
			return e
		}
		deadline := time.Now().Add(time.Duration(s.Shutdown) * time.Second)
		for o.Identity.alive() && time.Now().Before(deadline) {
			time.Sleep(50 * time.Millisecond)
		}
		if o.Identity.alive() {
			if e = syscall.Kill(-o.Identity.PID, syscall.SIGKILL); e != nil && e != syscall.ESRCH {
				return e
			}
		}
	}
	if e = stopContainer(o.Container, s.Shutdown); e != nil {
		return e
	}
	if e = os.Remove(path); os.IsNotExist(e) {
		return nil
	}
	return e
}
func releaseUnused(p Plan, force bool) error {
	used := map[string]bool{}
	var e error
	if !force {
		used, e = active(p)
		if e != nil {
			return e
		}
	}
	all := make([]string, 0, len(p.Services))
	for n := range p.Services {
		all = append(all, n)
	}
	sort.Strings(all)
	ordered, e := ordered(p, all)
	if e != nil {
		return e
	}
	live := controller(p).alive()
	for _, n := range ordered {
		if !used[n] && p.Services[n].Watch != nil {
			if e = watchStopping(p, n, true); e != nil {
				return e
			}
		}
	}
	for i := len(ordered) - 1; i >= 0; i-- {
		n := ordered[i]
		if used[n] {
			continue
		}
		if live {
			_, _ = backend(p, "process", "stop", n)
		}
		if e = stopOwner(p, n); e != nil {
			return e
		}
	}
	if len(used) == 0 {
		if live {
			_, _ = backend(p, "down")
		}
		id := controller(p)
		if id.alive() {
			_ = syscall.Kill(id.PID, syscall.SIGTERM)
		}
		_ = os.Remove(filepath.Join(p.State, "controller.json"))
		if socket, e := socketPath(p); e == nil {
			_ = os.Remove(socket)
		}
		if force {
			entries, _ := filepath.Glob(filepath.Join(p.State, "*.lease"))
			for _, path := range entries {
				_ = os.Remove(path)
			}
		}
	}
	return nil
}
func acquire(p Plan, persistent bool) (*os.File, string, error) {
	if e := bindEngines(&p); e != nil {
		return nil, "", e
	}
	if e := validate(p); e != nil {
		return nil, "", e
	}
	if e := private(p.State); e != nil {
		return nil, "", e
	}
	lock, e := locked(filepath.Join(p.State, "gate"), false)
	if e != nil {
		return nil, "", e
	}
	defer lock.Close()
	self, e := persistTools(&p)
	if e != nil {
		return nil, "", e
	}
	if e = expandWatches(&p, self); e != nil {
		return nil, "", e
	}
	var previous Plan
	previousUsers := map[string]bool{}
	e = readJSON(filepath.Join(p.State, "plan.json"), &previous)
	if e == nil {
		if previous.State != p.State || previous.Root != p.Root {
			return nil, "", fmt.Errorf("saved service scope does not match this project")
		}
		used, err := active(previous)
		previousUsers = used
		if err != nil {
			return nil, "", err
		}
		if len(used) > 0 && previous.Fingerprint != p.Fingerprint {
			return nil, "", fmt.Errorf("active services have incompatible inputs; stop their users before replacing them")
		}
		if len(used) == 0 && previous.Fingerprint != p.Fingerprint {
			if e = releaseUnused(previous, true); e != nil {
				return nil, "", e
			}
		}
		if !controller(previous).alive() {
			if len(used) > 0 {
				return nil, "", fmt.Errorf("service controller crashed while clients still hold leases; stop/status can recover it")
			}
			if e = releaseUnused(previous, true); e != nil {
				return nil, "", e
			}
		}
	} else if !os.IsNotExist(e) {
		return nil, "", e
	}
	selected, e := ordered(p, p.Requested)
	if e != nil {
		return nil, "", e
	}
	leasePath := filepath.Join(p.State, token()+".lease")
	if e = atomic(leasePath, Lease{Services: selected, Persistent: persistent, Container: p.TaskContainer}); e != nil {
		return nil, "", e
	}
	lease, e := locked(leasePath, false)
	if e != nil {
		return nil, "", e
	}
	cleanup := func(err error) (*os.File, string, error) {
		lease.Close()
		_ = os.Remove(leasePath)
		_ = releaseUnused(p, false)
		return nil, "", err
	}
	if !controller(p).alive() {
		if e = start(p, self); e != nil {
			return cleanup(e)
		}
	} else {
		if e = startMissing(p, selected, previousUsers); e != nil {
			return cleanup(e)
		}
	}
	if e = ready(p, selected); e != nil {
		return cleanup(e)
	}
	return lease, leasePath, nil
}
func persistTools(p *Plan) (string, error) {
	self, e := os.Executable()
	if e != nil {
		return "", e
	}
	assets := filepath.Join(p.State, "assets")
	if e = private(assets); e != nil {
		return "", e
	}
	for name, body := range p.Licenses {
		if !validName.MatchString(name) || len(body) > 1<<20 {
			return "", fmt.Errorf("invalid license asset")
		}
		if e = os.WriteFile(filepath.Join(assets, name+"-LICENSE"), []byte(body), 0600); e != nil {
			return "", e
		}
	}
	installed := []string{}
	sources := []string{self, p.Backend}
	if p.Watcher != "" {
		sources = append(sources, p.Watcher)
	}
	for _, source := range sources {
		f, e := os.OpenFile(source, os.O_RDONLY|syscall.O_NOFOLLOW, 0)
		if e != nil {
			return "", e
		}
		st, e := f.Stat()
		if e != nil {
			f.Close()
			return "", e
		}
		if !st.Mode().IsRegular() || st.Size() > 100<<20 {
			f.Close()
			return "", fmt.Errorf("invalid controller executable")
		}
		data, e := io.ReadAll(f)
		f.Close()
		if e != nil {
			return "", e
		}
		digest := sha256.Sum256(data)
		destination := filepath.Join(assets, hex.EncodeToString(digest[:]))
		f, e = os.OpenFile(destination, os.O_RDONLY|syscall.O_NOFOLLOW, 0)
		if e == nil {
			existing, err := io.ReadAll(io.LimitReader(f, 100<<20))
			f.Close()
			if err != nil {
				return "", err
			}
			if sha256.Sum256(existing) != digest {
				return "", fmt.Errorf("cached controller executable failed integrity check")
			}
		}
		if os.IsNotExist(e) {
			f, e = os.OpenFile(destination, os.O_CREATE|os.O_EXCL|os.O_WRONLY|syscall.O_NOFOLLOW, 0700)
			if e != nil {
				return "", e
			}
			_, e = f.Write(data)
			closeError := f.Close()
			if e != nil {
				os.Remove(destination)
				return "", e
			}
			if closeError != nil {
				return "", closeError
			}
		} else if e != nil {
			return "", e
		}
		installed = append(installed, destination)
	}
	p.Backend = installed[1]
	if p.Watcher != "" {
		p.Watcher = installed[2]
	}
	return installed[0], nil
}
func owned(state, name string, probe bool) int {
	if !validName.MatchString(name) {
		fmt.Fprintln(os.Stderr, "invalid service name")
		return 1
	}
	var s Service
	if e := readJSON(filepath.Join(state, name+".command.json"), &s); e != nil {
		fmt.Fprintln(os.Stderr, e)
		return 1
	}
	if e := checkEngine(s.Container); e != nil {
		fmt.Fprintln(os.Stderr, e)
		return 1
	}
	command := s.Command
	if probe {
		if s.Readiness == nil {
			return 0
		}
		command = s.Readiness.Command
	}
	cmd, e := child(command)
	if e != nil {
		fmt.Fprintln(os.Stderr, e)
		return 1
	}
	if s.ForwardLeases {
		if e = forwardLeases(cmd); e != nil {
			return exitCode(e)
		}
		defer closeForwarded(cmd)
	}
	if probe {
		return exitCode(cmd.Run())
	}
	if strings.HasSuffix(name, watchSuffix) {
		_ = os.Remove(filepath.Join(state, strings.TrimSuffix(name, watchSuffix)+".built.json"))
	}
	// Keep the identity anchor alive even after the application's direct process
	// exits. Foreground service descendants inherit this process group.
	if e = syscall.Setpgid(0, 0); e != nil && e != syscall.EPERM {
		fmt.Fprintln(os.Stderr, e)
		return 1
	}
	if syscall.Getpgrp() != os.Getpid() {
		fmt.Fprintln(os.Stderr, "service does not own its process group")
		return 1
	}
	id, e := identify(os.Getpid())
	if e != nil {
		fmt.Fprintln(os.Stderr, e)
		return 1
	}
	signals := make(chan os.Signal, 8)
	signal.Notify(signals, syscall.SIGINT, syscall.SIGTERM, syscall.SIGHUP)
	defer signal.Stop(signals)
	path := filepath.Join(state, name+".owner.json")
	if e = atomic(path, Owner{id, s.Container}); e != nil {
		fmt.Fprintln(os.Stderr, e)
		return 1
	}
	if e = cmd.Start(); e != nil {
		_ = os.Remove(path)
		fmt.Fprintln(os.Stderr, e)
		return 1
	}
	done := make(chan error, 1)
	go func() { done <- cmd.Wait() }()
	var timeout <-chan time.Time
	if s.Timeout > 0 {
		timer := time.NewTimer(time.Duration(s.Timeout) * time.Second)
		defer timer.Stop()
		timeout = timer.C
	}
	timedOut := false
	select {
	case e = <-done:
	case <-timeout:
		timedOut = true
		_ = syscall.Kill(-id.PID, syscall.SIGTERM)
		select {
		case e = <-done:
		case <-time.After(time.Duration(s.Shutdown) * time.Second):
			killMembers(id.PID, syscall.SIGKILL)
			e = <-done
		}
	case sig := <-signals:
		_ = syscall.Kill(-id.PID, sig.(syscall.Signal))
		select {
		case e = <-done:
		case <-time.After(time.Duration(s.Shutdown) * time.Second):
			killMembers(id.PID, syscall.SIGKILL)
			e = <-done
		}
	}
	killMembers(id.PID, syscall.SIGTERM)
	deadline := time.Now().Add(time.Duration(s.Shutdown) * time.Second)
	for time.Now().Before(deadline) {
		members, err := groupMembers(id.PID)
		if err != nil {
			fmt.Fprintln(os.Stderr, err)
			return 1
		}
		live := false
		for _, pid := range members {
			if pid != id.PID {
				if _, err := birth(pid); err == nil {
					live = true
				}
			}
		}
		if !live {
			break
		}
		time.Sleep(50 * time.Millisecond)
	}
	killMembers(id.PID, syscall.SIGKILL)
	if s.Container != nil {
		if err := stopContainer(s.Container, s.Shutdown); err != nil {
			fmt.Fprintln(os.Stderr, err)
			return 1
		}
	}
	_ = os.Remove(path)
	if timedOut {
		return 124
	}
	return exitCode(e)
}
func killMembers(group int, sig syscall.Signal) {
	members, e := groupMembers(group)
	if e != nil {
		fmt.Fprintln(os.Stderr, e)
		return
	}
	for _, pid := range members {
		if pid == os.Getpid() {
			continue
		}
		i, e := identify(pid)
		if e != nil {
			continue
		}
		g, e := syscall.Getpgid(pid)
		if e == nil && g == group && i.alive() {
			_ = syscall.Kill(pid, sig)
		}
	}
}
func exitCode(e error) int {
	if e == nil {
		return 0
	}
	var x *exec.ExitError
	if errors.As(e, &x) {
		if x.ExitCode() < 0 {
			return 128 + int(x.Sys().(syscall.WaitStatus).Signal())
		}
		return x.ExitCode()
	}
	fmt.Fprintln(os.Stderr, e)
	return 1
}
func mainAction(args []string) (result int) {
	if len(args) < 2 {
		fmt.Fprintln(os.Stderr, "usage: chainman-control run|up PLAN; status|stop STATE")
		return 2
	}
	if args[0] == "command" || args[0] == "sequence" {
		if len(args) != 2 {
			return 2
		}
		return taskCommand(args[0], args[1])
	}
	if args[0] == "build" || args[0] == "built" {
		if len(args) != 3 || !validName.MatchString(args[2]) {
			return 2
		}
		return watchAction(args[0], args[1], args[2])
	}
	if args[0] == "exec" || args[0] == "probe" {
		if len(args) != 3 {
			return 2
		}
		return owned(args[1], args[2], args[0] == "probe")
	}
	if args[0] == "status" || args[0] == "stop" {
		if e := private(args[1]); e != nil {
			fmt.Fprintln(os.Stderr, e)
			return 1
		}
		lock, e := locked(filepath.Join(args[1], "gate"), false)
		if e != nil {
			fmt.Fprintln(os.Stderr, e)
			return 1
		}
		defer lock.Close()
		var p Plan
		if e = readJSON(filepath.Join(args[1], "plan.json"), &p); os.IsNotExist(e) {
			fmt.Println("{\"services\":[],\"running\":false}")
			return 0
		}
		if e != nil {
			fmt.Fprintln(os.Stderr, e)
			return 1
		}
		if p.State != args[1] {
			fmt.Fprintln(os.Stderr, "saved service scope does not match requested state")
			return 1
		}
		if args[0] == "stop" {
			if e = releaseUnused(p, true); e != nil {
				fmt.Fprintln(os.Stderr, e)
				return 1
			}
			return 0
		}
		used, e := active(p)
		if e != nil {
			fmt.Fprintln(os.Stderr, e)
			return 1
		}
		ps, backendError := states(p)
		_ = json.NewEncoder(os.Stdout).Encode(map[string]any{"running": controller(p).alive(), "leases": used, "services": ps, "log": filepath.Join(p.State, "services.log"), "recovery_required": backendError != nil && len(used) > 0})
		return 0
	}
	if args[0] != "run" && args[0] != "up" {
		return 2
	}
	var p Plan
	if e := readJSON(args[1], &p); e != nil {
		fmt.Fprintln(os.Stderr, e)
		return 1
	}
	if p.Prepare != nil {
		cmd, e := child(*p.Prepare)
		if e != nil {
			return exitCode(e)
		}
		if e = cmd.Run(); e != nil {
			return exitCode(e)
		}
	}
	lease, _, e := acquire(p, args[0] == "up")
	if e != nil {
		fmt.Fprintln(os.Stderr, e)
		return 1
	}
	if args[0] == "up" {
		lease.Close()
		fmt.Println(p.State)
		return 0
	}
	defer func() {
		// Docker/Podman clients do not carry descriptors into their daemon's
		// containers. Stop an owned task before releasing its service lease;
		// if this client is killed outright, active() uses the container receipt.
		if err := stopContainer(p.TaskContainer, 10); err != nil {
			fmt.Fprintln(os.Stderr, err)
			if result == 0 {
				result = 1
			}
			lease.Close()
			return
		}
		lease.Close()
		lock, err := locked(filepath.Join(p.State, "gate"), false)
		if err == nil {
			defer lock.Close()
			var saved Plan
			err = readJSON(filepath.Join(p.State, "plan.json"), &saved)
			if err == nil && saved.State != p.State {
				err = fmt.Errorf("saved service scope changed")
			}
			if err == nil {
				err = releaseUnused(saved, false)
			}
			if err != nil {
				fmt.Fprintln(os.Stderr, err)
				if result == 0 {
					result = 1
				}
			}
		} else {
			fmt.Fprintln(os.Stderr, err)
			if result == 0 {
				result = 1
			}
		}
	}()
	cmd, e := child(p.Task)
	if e != nil {
		return exitCode(e)
	}
	// A task that survives its caller retains this resource lease. Process Compose
	// deliberately receives no client lease, so dead clients cannot pin services.
	cmd.ExtraFiles = []*os.File{lease}
	signals := make(chan os.Signal, 2)
	signal.Notify(signals, syscall.SIGINT, syscall.SIGTERM)
	defer signal.Stop(signals)
	if e = cmd.Start(); e != nil {
		return exitCode(e)
	}
	// A foreground Nix/shell entry may close inherited descriptors. Its kernel
	// identity is a second lease witness until the complete task returns.
	if identity, err := identify(cmd.Process.Pid); err == nil {
		gate, err := locked(filepath.Join(p.State, "gate"), false)
		if err == nil {
			selected, orderErr := ordered(p, p.Requested)
			if orderErr == nil {
				// Keep the locked inode: replacing it would disconnect inherited
				// descriptor leases from the receipt observed by other clients.
				_, err = lease.Seek(0, 0)
				if err == nil {
					err = lease.Truncate(0)
				}
				if err == nil {
					err = json.NewEncoder(lease).Encode(Lease{Services: selected, Container: p.TaskContainer, Task: &identity})
				}
				if err == nil {
					err = lease.Sync()
				}
			} else {
				err = orderErr
			}
			gate.Close()
		}
		if err != nil {
			fmt.Fprintln(os.Stderr, "task ownership receipt:", err)
		}
	}
	done := make(chan error, 1)
	go func() { done <- cmd.Wait() }()
	select {
	case e = <-done:
	case sig := <-signals:
		_ = cmd.Process.Signal(sig)
		e = <-done
	}
	return exitCode(e)
}
func main() { os.Exit(mainAction(os.Args[1:])) }
