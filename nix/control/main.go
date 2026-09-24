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
	"net/url"
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
	Command  Command    `json:"command"`
	HTTPGet  *HTTPProbe `json:"http_get,omitempty"`
	Period   int        `json:"period_seconds"`
	Timeout  int        `json:"timeout_seconds"`
	Failures int        `json:"failure_threshold"`
}
type HTTPProbe struct {
	Port       int    `json:"port"`
	Path       string `json:"path"`
	StatusCode int    `json:"status_code"`
}
type Service struct {
	Command      Command  `json:"command"`
	Dependencies []string `json:"depends_on"`
	Readiness    *Probe   `json:"readiness,omitempty"`
	Restart      string   `json:"restart"`
	Shutdown     int      `json:"shutdown_seconds"`
	// Container cleanup addresses an immutable engine ID after checking its label.
	Container      *Container `json:"container,omitempty"`
	Watch          *Watch     `json:"watch,omitempty"`
	Timeout        int        `json:"timeout_seconds,omitempty"`
	ForwardLeases  bool       `json:"forward_leases,omitempty"`
	Generation     string     `json:"generation,omitempty"`
	NetworkService string     `json:"network_service,omitempty"`
}
type Container struct {
	Engine         string `json:"engine"`
	Name           string `json:"name"`
	Token          string `json:"token"`
	EngineIdentity string `json:"engine_identity,omitempty"`
}
type Plan struct {
	Schema             int                `json:"schema"`
	Root               string             `json:"root"`
	State              string             `json:"state"`
	Backend            string             `json:"backend"`
	Watcher            string             `json:"watcher,omitempty"`
	Licenses           map[string]string  `json:"licenses,omitempty"`
	Fingerprint        string             `json:"fingerprint"`
	Services           map[string]Service `json:"services"`
	Volumes            []Volume           `json:"volumes,omitempty"`
	Bridge             *Bridge            `json:"bridge,omitempty"`
	Requested          []string           `json:"requested"`
	Task               Command            `json:"task"`
	Prepare            *Command           `json:"prepare,omitempty"`
	TaskContainer      *Container         `json:"task_container,omitempty"`
	WaitForServices    bool               `json:"wait_for_services,omitempty"`
	Presentation       Presentation       `json:"presentation,omitempty"`
	ExclusiveServices  bool               `json:"exclusive_services,omitempty"`
	OwnTask            bool               `json:"own_task,omitempty"`
	TaskShutdown       int                `json:"task_shutdown_seconds,omitempty"`
	Resources          []Plan             `json:"resources,omitempty"`
	Generation         string             `json:"generation,omitempty"`
	TaskNetworkService string             `json:"task_network_service,omitempty"`
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
	Services          []string   `json:"services"`
	ExclusiveServices bool       `json:"exclusive_services,omitempty"`
	Persistent        bool       `json:"persistent"`
	Container         *Container `json:"container,omitempty"`
	Task              *Identity  `json:"task,omitempty"`
	// The explicit field also makes older readers reject this receipt instead
	// of overlooking a task identity they do not know how to inspect.
	TaskReceipt bool      `json:"task_receipt,omitempty"`
	Parent      *LeaseRef `json:"parent,omitempty"`
}
type Process struct {
	Name     string `json:"name"`
	Status   string `json:"status"`
	Ready    string `json:"is_ready"`
	Running  bool   `json:"is_running"`
	ExitCode *int   `json:"exit_code,omitempty"`
}

var validName = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_-]*$`)

func readJSON(path string, v any) error {
	f, err := os.OpenFile(path, os.O_RDONLY|syscall.O_NOFOLLOW|syscall.O_NONBLOCK, 0)
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
func private(path string) error         { return stateDirectory(path, true) }
func existingPrivate(path string) error { return stateDirectory(path, false) }
func stateDirectory(path string, create bool) error {
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
	if create {
		if err := os.MkdirAll(path, 0700); err != nil {
			return err
		}
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

// Bound pipe draining as well as process lifetime if an engine leaves a child
// holding its query output open after cancellation.
func queryCommand(ctx context.Context, executable string, args ...string) *exec.Cmd {
	cmd := exec.CommandContext(ctx, executable, args...)
	cmd.WaitDelay = 100 * time.Millisecond
	return cmd
}
func backend(p Plan, args ...string) ([]byte, error) {
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	return backendContext(ctx, p, args...)
}
func backendContext(ctx context.Context, p Plan, args ...string) ([]byte, error) {
	socket, e := socketLocation(p)
	if e != nil {
		return nil, e
	}
	if e := existingPrivate(filepath.Dir(socket)); e != nil {
		return nil, e
	}
	base := []string{"--use-uds", "--unix-socket", socket, "--log-file", os.DevNull}
	cmd := queryCommand(ctx, p.Backend, append(base, args...)...)
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
	path, err := socketLocation(p)
	if err == nil {
		err = private(filepath.Dir(path))
	}
	return path, err
}
func socketLocation(p Plan) (string, error) {
	base, e := filepath.EvalSymlinks("/tmp")
	if e != nil {
		return "", e
	}
	directory := filepath.Join(base, fmt.Sprintf("chainman-control-%d", os.Geteuid()))
	if directory == p.Root || strings.HasPrefix(directory, p.Root+"/") {
		return "", fmt.Errorf("controller socket cannot be inside project mounts")
	}
	key := sha256.Sum256([]byte(p.State))
	return filepath.Join(directory, hex.EncodeToString(key[:12])+".sock"), nil
}
func states(p Plan) ([]Process, error) {
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	return statesContext(ctx, p)
}
func statesContext(ctx context.Context, p Plan) ([]Process, error) {
	if len(p.Services) == 0 {
		return []Process{}, nil
	}
	b, e := backendContext(ctx, p, "process", "list", "-o", "json")
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
	if len(p.Resources) > 2 {
		return fmt.Errorf("only repository data and network resource pools are supported")
	}
	if e := validateBridge(p.Bridge); e != nil {
		return e
	}
	if p.Bridge != nil && (len(p.Services) > 0 || len(p.Resources) > 0) {
		return fmt.Errorf("network scopes cannot contain services or nested resources")
	}
	resourceStates := map[string]bool{}
	for _, resource := range p.Resources {
		if len(resource.Resources) > 0 || resource.State == p.State || filepath.Dir(resource.State) != filepath.Dir(p.State) || resourceStates[resource.State] {
			return fmt.Errorf("invalid repository resource scope")
		}
		resourceStates[resource.State] = true
		if e := validate(resource); e != nil {
			return e
		}
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
			if r.HTTPGet != nil {
				h := r.HTTPGet
				u, err := url.ParseRequestURI(h.Path)
				if len(r.Command.Argv) != 0 || r.Command.Directory != "" || len(r.Command.Environment) != 0 || h.Port < 1 || h.Port > 65535 || h.StatusCode < 200 || h.StatusCode > 299 || err != nil || u.IsAbs() || !strings.HasPrefix(h.Path, "/") || strings.HasPrefix(h.Path, "//") || strings.ContainsAny(h.Path, "# \t\r\n") {
					return fmt.Errorf("invalid HTTP readiness declaration")
				}
			} else if _, e := child(r.Command); e != nil {
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
	generation := p.Generation
	for n, s := range p.Services {
		s.Generation = generation
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
		admission, e := locked(filepath.Join(p.State, n+".admission"), false)
		if e != nil {
			return e
		}
		e = atomic(spec, s)
		if e == nil {
			e = clearStopping(p, n)
		}
		admission.Close()
		if e != nil {
			return e
		}
		// The owner forwards graceful signals; group delivery here would signal
		// the application twice. Backend timeout escalation still kills the group.
		d := map[string]any{"command": "exec \"$CHAINMAN_CONTROL_EXECUTABLE\" exec \"$CHAINMAN_CONTROL_STATE\" " + quote(n) + " " + quote(generation), "is_template_disabled": true, "availability": map[string]any{"restart": s.Restart}, "shutdown": map[string]any{"timeout_seconds": s.Shutdown + 2, "parent_only": true}}
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
			// The native owner enforces the command deadline and reaps descendants.
			// Leave room for engine identity checks and bounded group cleanup before
			// the backend's last-resort deadline can kill the identity anchor.
			d["readiness_probe"] = map[string]any{"exec": map[string]string{"command": "exec \"$CHAINMAN_CONTROL_EXECUTABLE\" probe \"$CHAINMAN_CONTROL_STATE\" " + quote(n) + " " + quote(generation)}, "period_seconds": r.Period, "timeout_seconds": r.Timeout + 15, "failure_threshold": r.Failures}
			if r.HTTPGet != nil {
				h := r.HTTPGet
				d["readiness_probe"] = map[string]any{"http_get": map[string]any{"host": "127.0.0.1", "scheme": "http", "port": fmt.Sprint(h.Port), "path": h.Path, "status_code": h.StatusCode}, "period_seconds": r.Period, "timeout_seconds": r.Timeout, "failure_threshold": r.Failures}
			}
		}
		processes[n] = d
	}
	return atomic(filepath.Join(p.State, "compose.json"), map[string]any{
		"version": "0.5", "processes": processes, "log_location": "services.log", "log_length": 500,
		"log_configuration": map[string]any{"flush_each_line": true, "disable_json": false, "no_color": true, "add_timestamp": true, "rotation": map[string]any{"max_size_mb": 10, "max_backups": 3, "max_age_days": 7}},
	})
}
func start(p Plan, self string) error {
	admission, e := locked(filepath.Join(p.State, "controller.admission"), false)
	if e != nil {
		return e
	}
	defer admission.Close()
	p.Generation = token()
	if e := os.Remove(filepath.Join(p.State, "stop-request.json")); e != nil && !os.IsNotExist(e) {
		return e
	}
	if e := configuration(p, self); e != nil {
		return e
	}
	if e := atomic(filepath.Join(p.State, "plan.json"), p); e != nil {
		return e
	}
	admission.Close()
	log, e := os.OpenFile(os.DevNull, os.O_WRONLY, 0)
	if e != nil {
		return e
	}
	defer log.Close()
	cmd := exec.Command(self, "controller", filepath.Join(p.State, "plan.json"), p.Generation)
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
func ready(p Plan, names []string, startup *startupGuard) error {
	defer tracePhase("service_readiness")()
	if e := startup.check(); e != nil {
		return e
	}
	if len(names) == 0 {
		return nil
	}
	// Probe execution, thresholds and dependency state are owned by Process Compose.
	// This deadline only bounds a failed/lost controller, not a second probe loop.
	seconds := 15
	for _, n := range names {
		if r := p.Services[n].Readiness; r != nil {
			seconds += (r.Period + r.Timeout + 15) * r.Failures
		}
	}
	deadline := time.Now().Add(time.Duration(seconds) * time.Second)
	for time.Now().Before(deadline) {
		if e := startup.check(); e != nil {
			return e
		}
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
				detail := s.Status
				if s.ExitCode != nil {
					detail += fmt.Sprintf(", exit code %d", *s.ExitCode)
				}
				return fmt.Errorf("service %s exited before readiness (%s); log: %s", n, detail, filepath.Join(p.State, "services.log"))
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
		if l.Parent != nil {
			present, err := parentAlive(p, *l.Parent)
			if err != nil {
				return nil, err
			}
			if !present {
				if e = removeLease(path); e != nil {
					return nil, e
				}
				continue
			}
			for _, n := range l.Services {
				result[n] = true
			}
			continue
		}
		f, err := locked(path, true)
		if err == nil {
			f.Close()
			if !l.Persistent {
				present, err := leaseTaskAlive(path, l)
				if err != nil {
					return nil, err
				}
				if !present && l.Container != nil {
					info, err := inspectContainer(l.Container)
					if err != nil {
						return nil, err
					}
					present = info != nil && info.Running
				}
				if !present {
					if e = removeLease(path); e != nil {
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
	return containerStateContext(ctx, c)
}
func inspectContainerContext(ctx context.Context, c *Container) (*ContainerState, error) {
	if e := checkEngineContext(ctx, c); e != nil {
		return nil, e
	}
	return containerStateContext(ctx, c)
}
func containerStateContext(ctx context.Context, c *Container) (*ContainerState, error) {
	out, e := queryCommand(ctx, c.Engine, "container", "inspect", c.Name).Output()
	if e != nil {
		// An engine outage is not evidence of absence. A separate list must succeed.
		list, err := queryCommand(ctx, c.Engine, "container", "ls", "-aq", "--filter", "name=^/"+c.Name+"$").Output()
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

func containerProbe(c *Container, command Command) (Command, error) {
	if c == nil {
		return command, nil
	}
	if len(command.Argv) < 4 || command.Argv[0] != c.Engine || command.Argv[1] != "exec" || command.Argv[2] != c.Name {
		return Command{}, fmt.Errorf("container probe must execute in its declared owner")
	}
	info, e := inspectContainer(c)
	if e != nil {
		return Command{}, e
	}
	if info == nil || !info.Running {
		return Command{}, fmt.Errorf("container probe owner is not running")
	}
	// A replacement can reuse the name even after inspection. Bind this exec to
	// the owned immutable ID without rewriting any of the probe's literal args.
	command.Argv = append([]string(nil), command.Argv...)
	command.Argv[2] = info.ID
	return command, nil
}

func engineIdentity(engine string) (string, error) {
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	return engineIdentityContext(ctx, engine)
}
func engineIdentityContext(ctx context.Context, engine string) (string, error) {
	format := "{{.ID}}"
	if filepath.Base(engine) == "podman" {
		format = "{{.Host.Hostname}} {{.Store.GraphRoot}} {{.Host.Security.Rootless}}"
	} else if filepath.Base(engine) != "docker" {
		return "", fmt.Errorf("unsupported host engine")
	}
	data, e := queryCommand(ctx, engine, "info", "--format", format).Output()
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
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	return checkEngineContext(ctx, c)
}
func checkEngineContext(ctx context.Context, c *Container) error {
	if c == nil || c.EngineIdentity == "" {
		return nil
	}
	current, e := engineIdentityContext(ctx, c.Engine)
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
	// A repository-only `up` has no local container, while its later `run` does.
	// Bind the already declared resource engines in both cases so adding a task
	// does not change the identity of otherwise compatible live services.
	for _, resource := range p.Resources {
		for _, service := range resource.Services {
			if service.Container != nil {
				containers = append(containers, &Container{Engine: service.Container.Engine})
			}
		}
		for _, volume := range resource.Volumes {
			containers = append(containers, &Container{Engine: volume.Engine})
		}
		if resource.Bridge != nil {
			containers = append(containers, &Container{Engine: resource.Bridge.Engine})
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
	for index := range p.Volumes {
		volume := &p.Volumes[index]
		identity, ok := identities[volume.Engine]
		if !ok {
			var e error
			identity, e = engineIdentity(volume.Engine)
			if e != nil {
				return e
			}
			identities[volume.Engine] = identity
		}
		volume.EngineIdentity = identity
	}
	if p.Bridge != nil {
		identity, ok := identities[p.Bridge.Engine]
		if !ok {
			var e error
			identity, e = engineIdentity(p.Bridge.Engine)
			if e != nil {
				return e
			}
			identities[p.Bridge.Engine] = identity
		}
		p.Bridge.EngineIdentity = identity
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
	if info == nil || !info.Running {
		return nil
	}
	id := info.ID
	ctx, cancel := context.WithTimeout(context.Background(), time.Duration(seconds+15)*time.Second)
	defer cancel()
	cmd := exec.CommandContext(ctx, c.Engine, "container", "stop", "--time", fmt.Sprint(seconds), id)
	if b, e := cmd.CombinedOutput(); e != nil {
		// The foreground client and explicit recovery can finish together.
		// Reconfirm absence/stopped state; never confuse an engine outage with it.
		if current, err := inspectContainer(c); err == nil && (current == nil || !current.Running) {
			return nil
		}
		return fmt.Errorf("owned container stop: %w: %s", e, b)
	}
	return nil
}
func stopOwner(p Plan, n string) error {
	if e := stopReceipt(p, n, n+".probe.owner.json", 1); e != nil {
		return e
	}
	return stopReceipt(p, n, n+".owner.json", p.Services[n].Shutdown)
}
func stopReceipt(p Plan, n, receipt string, shutdown int) error {
	path := filepath.Join(p.State, receipt)
	var o Owner
	e := readJSON(path, &o)
	if os.IsNotExist(e) {
		return nil
	}
	if e != nil {
		return e
	}
	if o.Identity.alive() {
		// The native owner forwards this signal to its application group once.
		if e = syscall.Kill(o.Identity.PID, syscall.SIGTERM); e != nil && e != syscall.ESRCH {
			return e
		}
		// A job-control stopped owner cannot forward termination until resumed.
		_ = syscall.Kill(o.Identity.PID, syscall.SIGCONT)
		deadline := time.Now().Add(time.Duration(shutdown) * time.Second)
		for o.Identity.alive() && time.Now().Before(deadline) {
			time.Sleep(50 * time.Millisecond)
		}
		if o.Identity.alive() {
			if e = syscall.Kill(-o.Identity.PID, syscall.SIGKILL); e != nil && e != syscall.ESRCH {
				return e
			}
		}
	}
	if e = stopContainer(o.Container, shutdown); e != nil {
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
	if force || (len(used) == 0 && !hasLeases(p)) {
		admission, e := locked(filepath.Join(p.State, "controller.admission"), false)
		if e != nil {
			return e
		}
		e = atomic(filepath.Join(p.State, "stop-request.json"), true)
		admission.Close()
		if e != nil {
			return e
		}
	}
	if force {
		if e = stopTasks(p); e != nil {
			return e
		}
		// Older receipts may predate durable task anchors. Preserve their
		// immutable engine/container witness until recovery has succeeded.
		entries, _ := filepath.Glob(filepath.Join(p.State, "*.lease"))
		for _, path := range entries {
			var lease Lease
			if err := readJSON(path, &lease); err != nil {
				continue
			}
			if e = stopContainer(lease.Container, 10); e != nil {
				return e
			}
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
		if !used[n] {
			if e = serviceStopping(p, n, true); e != nil {
				return e
			}
		}
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
				_ = removeLease(path)
			}
		}
	}
	if e = pruneResources(p); e != nil {
		return e
	}
	if p.Bridge != nil && !hasLeases(p) {
		return removeBridge(p.Bridge)
	}
	return nil
}
func acquire(p Plan, persistent bool, parent *LeaseRef, startup *startupGuard) (*os.File, string, error) {
	if e := bindEngines(&p); e != nil {
		return nil, "", e
	}
	if e := validate(p); e != nil {
		return nil, "", e
	}
	if e := private(p.State); e != nil {
		return nil, "", e
	}
	if e := startup.observe(p.State); e != nil {
		return nil, "", e
	}
	lock, e := startup.lock(filepath.Join(p.State, "gate"))
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
	previousClients := false
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
		previousClients = len(used) > 0 || hasLeases(previous)
		if previousClients && previous.Fingerprint != p.Fingerprint {
			return nil, "", fmt.Errorf("active services have incompatible inputs (running %q, requested %q); inspect services-status and stop their users before replacing them", previous.Fingerprint, p.Fingerprint)
		}
		if !previousClients && previous.Fingerprint != p.Fingerprint {
			if e = releaseUnused(previous, true); e != nil {
				return nil, "", e
			}
		}
		if len(previous.Services) > 0 && !controller(previous).alive() {
			if len(used) > 0 {
				return nil, "", fmt.Errorf("service controller crashed while clients still hold leases; stop/status can recover it")
			}
			if !previousClients {
				if e = releaseUnused(previous, true); e != nil {
					return nil, "", e
				}
			}
		}
	} else if !os.IsNotExist(e) {
		return nil, "", e
	}
	selected, e := ordered(p, p.Requested)
	if e != nil {
		return nil, "", e
	}
	// active() has already removed dead claims under this scope's gate. Compare
	// the remaining claims before volume preparation or any service mutation.
	if e = checkServiceAccess(p, selected); e != nil {
		return nil, "", e
	}
	needed := []Volume{}
	for _, volume := range p.Volumes {
		for _, name := range selected {
			found := false
			for _, service := range volume.Services {
				if service == name {
					found = true
				}
			}
			if found {
				needed = append(needed, volume)
				break
			}
		}
	}
	for _, volume := range needed {
		inUse := false
		for _, name := range volume.Services {
			inUse = inUse || previousUsers[name]
		}
		if e = ensureVolumes([]Volume{volume}, inUse); e != nil {
			return nil, "", e
		}
	}
	leasePath := filepath.Join(p.State, token()+".lease")
	if e = atomic(leasePath, Lease{Services: selected, ExclusiveServices: p.ExclusiveServices, Persistent: persistent, Container: p.TaskContainer, Parent: parent, TaskReceipt: !persistent && parent == nil}); e != nil {
		return nil, "", e
	}
	lease, e := locked(leasePath, false)
	if e != nil {
		return nil, "", e
	}
	cleanup := func(err error) (*os.File, string, error) {
		lease.Close()
		_ = removeLease(leasePath)
		_ = releaseUnused(p, false)
		return nil, "", err
	}
	// Persist resource intent before acquiring anything in another scope. Recovery
	// can then reap its parent-linked claims even if this client dies during start.
	// An empty local selection can still have live repository users without a
	// local controller. Retain their cleanup intent when adding another task.
	if controller(p).alive() {
		previous.Resources = mergeResources(previous.Resources, p.Resources)
		if e = atomic(filepath.Join(p.State, "plan.json"), previous); e != nil {
			return cleanup(e)
		}
	} else {
		saved := p
		if previousClients {
			saved.Resources = mergeResources(previous.Resources, p.Resources)
		}
		if e = atomic(filepath.Join(p.State, "plan.json"), saved); e != nil {
			return cleanup(e)
		}
	}
	// The intent and lease are durable before the engine creates anything.
	if p.Bridge != nil {
		if e = ensureBridge(p.Bridge, previousClients); e != nil {
			return cleanup(e)
		}
	}
	for _, resource := range p.Resources {
		if len(resource.Requested) == 0 && resource.Bridge == nil {
			continue
		}
		claim, _, err := acquire(resource, false, &LeaseRef{State: p.State, File: filepath.Base(leasePath)}, startup)
		if err != nil {
			return cleanup(err)
		}
		claim.Close()
	}
	if e = startup.check(); e != nil {
		return cleanup(e)
	}
	if len(selected) == 0 {
		if e = os.Remove(filepath.Join(p.State, "stop-request.json")); e != nil && !os.IsNotExist(e) {
			return cleanup(e)
		}
	} else if !controller(p).alive() {
		if e = start(p, self); e != nil {
			return cleanup(e)
		}
	} else {
		if e = startMissing(p, selected, previousUsers); e != nil {
			return cleanup(e)
		}
	}
	if e = ready(p, selected, startup); e != nil {
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
func owned(state, name string, probe bool, generation string) int {
	if !validName.MatchString(name) {
		fmt.Fprintln(os.Stderr, "invalid service name")
		return 1
	}
	admission, e := locked(filepath.Join(state, name+".admission"), false)
	if e != nil {
		return exitCode(e)
	}
	defer admission.Close()
	var s Service
	if e := readJSON(filepath.Join(state, name+".command.json"), &s); e != nil {
		fmt.Fprintln(os.Stderr, e)
		return 1
	}
	if s.Generation != generation {
		return exitCode(fmt.Errorf("service generation changed before admission"))
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
		if s.Readiness.HTTPGet != nil {
			return exitCode(fmt.Errorf("HTTP readiness is evaluated by Process Compose"))
		}
		command, e = serviceProbe(name, s)
		if e != nil {
			return exitCode(e)
		}
	}
	if !probe && s.NetworkService != "" {
		var plan Plan
		if e := readJSON(filepath.Join(state, "plan.json"), &plan); e != nil {
			return exitCode(e)
		}
		var e error
		command, e = joinNetwork(plan, s.NetworkService, s.Container, command)
		if e != nil {
			return exitCode(e)
		}
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
		// Probes have the same process-group ownership as services, with a
		// distinct receipt so timeout or recovery cannot stop the application.
		s.Timeout = s.Readiness.Timeout
		s.Shutdown = 1
		s.Container = nil
	}
	if !probe && strings.HasSuffix(name, watchSuffix) {
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
	if probe {
		path = filepath.Join(state, name+".probe.owner.json")
	}
	if e = atomic(path, Owner{id, s.Container}); e != nil {
		fmt.Fprintln(os.Stderr, e)
		return 1
	}
	// Register before observing the stop marker. Stop first publishes that marker,
	// then reads this receipt, so a delayed launch is either found or refused.
	if _, e = os.Stat(filepath.Join(state, name+".stopping")); e == nil {
		_ = os.Remove(path)
		return 0
	} else if !os.IsNotExist(e) {
		return exitCode(e)
	}
	var current Service
	if e = readJSON(filepath.Join(state, name+".command.json"), &current); e != nil || current.Generation != generation {
		return exitCode(fmt.Errorf("service generation changed during admission"))
	}
	if e = cmd.Start(); e != nil {
		_ = os.Remove(path)
		fmt.Fprintln(os.Stderr, e)
		return 1
	}
	admission.Close()
	done := make(chan error, 1)
	go func() { done <- cmd.Wait() }()
	var timeout <-chan time.Time
	if s.Timeout > 0 {
		timer := time.NewTimer(time.Duration(s.Timeout) * time.Second)
		defer timer.Stop()
		timeout = timer.C
	}
	timedOut := false
	var interrupted syscall.Signal
	select {
	case e = <-done:
	case <-timeout:
		timedOut = true
		_ = syscall.Kill(-id.PID, syscall.SIGTERM)
		_ = syscall.Kill(-id.PID, syscall.SIGCONT)
		select {
		case e = <-done:
		case <-time.After(time.Duration(s.Shutdown) * time.Second):
			killMembers(id.PID, syscall.SIGKILL)
			e = <-done
		}
	case sig := <-signals:
		interrupted = sig.(syscall.Signal)
		if s.ForwardLeases {
			fmt.Fprintln(os.Stderr, "chainman: stopping task…")
		}
		_ = syscall.Kill(-id.PID, sig.(syscall.Signal))
		_ = syscall.Kill(-id.PID, syscall.SIGCONT)
		select {
		case e = <-done:
		case <-time.After(time.Duration(s.Shutdown) * time.Second):
			killMembers(id.PID, syscall.SIGKILL)
			e = <-done
		}
	}
	if e := finishGroup(id.PID, time.Now().Add(time.Duration(s.Shutdown)*time.Second)); e != nil {
		fmt.Fprintln(os.Stderr, e)
		return 1
	}
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
	if s.ForwardLeases {
		// Cancellation is an operation result, even if a cooperative child
		// exits zero or preparation finishes while the interrupt is in flight.
		// Otherwise the caller can admit another task or wait on services.
		if interrupted == 0 {
			select {
			case sig := <-signals:
				interrupted = sig.(syscall.Signal)
			default:
			}
		}
		if interrupted != 0 {
			return 128 + int(interrupted)
		}
	}
	return exitCode(e)
}

// The calling process remains alive as the group's identity anchor throughout.
func finishGroup(group int, deadline time.Time) error {
	killMembers(group, syscall.SIGTERM)
	for time.Now().Before(deadline) {
		members, err := groupMembers(group)
		if err != nil {
			return err
		}
		live := false
		for _, pid := range members {
			if pid != group {
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
	killMembers(group, syscall.SIGKILL)
	return nil
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
	var interrupted *startupInterrupted
	if errors.As(e, &interrupted) {
		return 128 + int(interrupted.signal)
	}
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
	if len(args) > 0 && args[0] == "setup-consent" {
		return consentAction(args[1:])
	}
	if len(args) > 0 && args[0] == "hook-exec" {
		return exitCode(hookExec(args[1:]))
	}
	if len(args) > 0 && args[0] == "hook" {
		return hookAction(args[1:])
	}
	if len(args) < 2 {
		fmt.Fprintln(os.Stderr, "usage: chainman-control run|up PLAN; status|stop STATE")
		return 2
	}
	if args[0] == "logs" {
		if len(args) != 2 && (len(args) != 3 || args[2] != "--follow") {
			return 2
		}
		return serviceLogs(args[1], len(args) == 3)
	}
	if args[0] == "command" || args[0] == "sequence" {
		if len(args) != 2 {
			return 2
		}
		return taskCommand(args[0], args[1])
	}
	if args[0] == "leased-task" && len(args) == 2 {
		return exitCode(leasedTask(args[1]))
	}
	if args[0] == "controller" && len(args) == 3 {
		return controllerExec(args[1], args[2])
	}
	if args[0] == "build" || args[0] == "built" {
		if len(args) != 3 || !validName.MatchString(args[2]) {
			return 2
		}
		return watchAction(args[0], args[1], args[2])
	}
	if args[0] == "exec" || args[0] == "probe" {
		if len(args) != 3 && len(args) != 4 {
			return 2
		}
		generation := ""
		if len(args) == 4 {
			generation = args[3]
		}
		return owned(args[1], args[2], args[0] == "probe", generation)
	}
	if args[0] == "status" {
		if len(args) != 2 && !(len(args) == 3 && args[2] == "--human") {
			return 2
		}
		return serviceStatus(args[1], len(args) == 3)
	}
	if args[0] == "stop" {
		if len(args) != 2 {
			return 2
		}
		if e := private(args[1]); e != nil {
			fmt.Fprintln(os.Stderr, e)
			return 1
		}
		stopping, e := locked(filepath.Join(args[1], "stop.admission"), false)
		if e != nil {
			return exitCode(e)
		}
		defer stopping.Close()
		notice := stopNotice{Token: token(), Pending: true}
		path := filepath.Join(args[1], "startup-stop.json")
		if e = atomic(path, notice); e != nil {
			return exitCode(e)
		}
		defer func() {
			notice.Pending = false
			if err := atomic(path, notice); err != nil {
				fmt.Fprintln(os.Stderr, err)
				result = 1
			}
		}()

		lock, e := locked(filepath.Join(args[1], "gate"), false)
		if e != nil {
			fmt.Fprintln(os.Stderr, e)
			return 1
		}
		defer lock.Close()
		var p Plan
		if e = readJSON(filepath.Join(args[1], "plan.json"), &p); os.IsNotExist(e) {
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
		if e = releaseUnused(p, true); e != nil {
			fmt.Fprintln(os.Stderr, e)
			return 1
		}
		return 0
	}
	if args[0] != "run" && args[0] != "up" && args[0] != "reset" {
		return 2
	}
	var p Plan
	if e := readJSON(args[1], &p); e != nil {
		fmt.Fprintln(os.Stderr, e)
		return 1
	}
	if args[0] == "reset" {
		if len(args) != 3 || args[2] != "--discard-data" {
			return 2
		}
		if e := bindEngines(&p); e != nil {
			return exitCode(e)
		}
		for i := range p.Resources {
			if e := bindEngines(&p.Resources[i]); e != nil {
				return exitCode(e)
			}
		}
		if e := resetVolumes(p); e != nil {
			fmt.Fprintln(os.Stderr, e)
			return 1
		}
		return 0
	}
	var development *developmentSession
	if args[0] == "run" {
		var err error
		development, err = beginDevelopment(&p)
		if err != nil {
			return exitCode(err)
		}
		defer func() { development.finish(result) }()
	}
	if p.Prepare != nil {
		if result := prepareCommand(*p.Prepare, p.TaskShutdown); result != 0 {
			return result
		}
	}
	signals := make(chan os.Signal, 2)
	signal.Notify(signals, syscall.SIGINT, syscall.SIGTERM, syscall.SIGHUP)
	defer signal.Stop(signals)
	startup := &startupGuard{signals: signals, stops: map[string]string{}}
	if p.WaitForServices && args[0] == "run" {
		cursors, err := liveLogCursors(p)
		if err != nil {
			return exitCode(err)
		}
		ctx, cancel := context.WithCancel(context.Background())
		go func() {
			if err := streamLogCursors(ctx, cursors, development.writer(), true); err != nil {
				fmt.Fprintln(os.Stderr, "Service log viewer:", err)
			}
		}()
		// A full caller pipe must not prevent task cancellation or service cleanup.
		// main exits after releasing ownership; it need not join a blocked viewer.
		defer cancel()
	}
	var startupLogs []*logCursor
	if !p.WaitForServices || args[0] != "run" {
		var err error
		startupLogs, err = liveLogCursors(p)
		if err != nil {
			return exitCode(err)
		}
		defer func() {
			for _, cursor := range startupLogs {
				cursor.close()
			}
		}()
	}
	lease, _, e := acquire(p, args[0] == "up", nil, startup)
	if e != nil {
		var interrupted *startupInterrupted
		if len(startupLogs) > 0 && !errors.Is(e, servicesStopped) && !errors.As(e, &interrupted) {
			startupDiagnostics(startupLogs)
		}
		return exitCode(e)
	}
	if args[0] == "up" {
		lease.Close()
		fmt.Println(p.State)
		return 0
	}
	recoveryState := ""
	defer func() {
		development.phase("stopping", "")
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
		if recoveryState != "" {
			// Bounded cancellation may have killed the task intermediary before
			// its identity anchor released inherited leases. Finish that exact
			// owned group before deciding which services still have clients.
			taskPlan := Plan{State: recoveryState, Services: map[string]Service{"task": {Shutdown: p.TaskShutdown}}}
			if err := stopOwner(taskPlan, "task"); err != nil {
				fmt.Fprintln(os.Stderr, err)
				if result == 0 {
					result = 1
				}
				lease.Close()
				return
			}
		}
		lease.Close()
		lock, err := locked(filepath.Join(p.State, "gate"), false)
		if err == nil {
			defer lock.Close()
			if recoveryState != "" {
				// Only discard a completed anchor. Forced stop holds this same
				// gate while recording admission markers and recovering owners.
				var owner Owner
				if e := readJSON(filepath.Join(recoveryState, "task.owner.json"), &owner); os.IsNotExist(e) {
					_ = os.RemoveAll(recoveryState)
				}
			}
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
	task := p.Task
	if p.TaskNetworkService != "" {
		// Read the saved plan: compatible reuse keeps the original service tokens.
		var saved Plan
		if e = readJSON(filepath.Join(p.State, "plan.json"), &saved); e != nil {
			return exitCode(e)
		}
		task, e = joinNetwork(saved, p.TaskNetworkService, p.TaskContainer, task)
		if e != nil {
			return exitCode(e)
		}
	}
	if p.OwnTask {
		if p.TaskShutdown < 1 || p.TaskShutdown > 300 {
			return exitCode(fmt.Errorf("invalid task shutdown timeout"))
		}
		path := filepath.Join(p.State, token()+".task.json")
		recovery, err := taskRecoveryState(p)
		if err != nil {
			return exitCode(err)
		}
		recoveryState = recovery
		if e = atomic(path, TaskCommands{Commands: []Command{task}, Shutdown: p.TaskShutdown, RecoveryState: recovery}); e != nil {
			return exitCode(e)
		}
		defer os.Remove(path)
		self, e := os.Executable()
		if e != nil {
			return exitCode(e)
		}
		task = Command{Argv: []string{self, "command", path}, Directory: p.Root, Environment: map[string]string{"CHAINMAN_SERVICE_LEASE_FDS": "[3]"}}
	}
	if e = atomic(lease.Name()+".command.json", task); e != nil {
		return exitCode(e)
	}
	self, e := os.Executable()
	if e != nil {
		return exitCode(e)
	}
	cmd, e := child(Command{Argv: []string{self, "leased-task", lease.Name()}})
	if e != nil {
		return exitCode(e)
	}
	// A task that survives its caller retains this resource lease. Process Compose
	// deliberately receives no client lease, so dead clients cannot pin services.
	cmd.ExtraFiles = []*os.File{lease}
	if e = cmd.Start(); e != nil {
		return exitCode(e)
	}
	development.phase("preparing", "")
	done := make(chan error, 1)
	go func() { done <- cmd.Wait() }()
	monitorContext, cancelMonitor := context.WithCancel(context.Background())
	defer cancelMonitor()
	failed := monitorServices(monitorContext, p, development)
	select {
	case e = <-done:
	case sig := <-signals:
		development.phase("stopping", "")
		fmt.Fprintln(os.Stderr, "chainman: stopping task and releasing its services…")
		e = cancelTask(cmd, done, sig, p.TaskShutdown)
	case e = <-failed:
		development.phase("stopping", "")
		_ = cancelTask(cmd, done, syscall.SIGTERM, p.TaskShutdown)
	}
	// Forced recovery may end the native task before the monitor's next tick.
	if _, stopped := os.Stat(filepath.Join(p.State, "stop-request.json")); stopped == nil {
		if p.WaitForServices {
			return 0
		}
		e = servicesStopped
	}
	return exitCode(e)
}
func main() { os.Exit(mainAction(os.Args[1:])) }
