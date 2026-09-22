package main

// Hooks use native Git and its configuration. Only fixed runtime phases cross
// into a managed environment; no container can submit arbitrary host commands.
import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"time"
	"unicode/utf8"
)

type HookPlan struct {
	Root      string            `json:"root"`
	Git       string            `json:"git"`
	Launcher  string            `json:"launcher"`
	Lefthook  string            `json:"lefthook"`
	Directory string            `json:"directory"`
	Enabled   bool              `json:"enabled"`
	Config    string            `json:"config"`
	Authority map[string][]byte `json:"authority"`
}
type hookFile struct {
	Body   []byte
	Mode   os.FileMode
	Exists bool
}

func hookPath(root, name string) (string, error) {
	if !utf8.ValidString(name) {
		return "", fmt.Errorf("hook paths must be valid UTF-8")
	}
	if filepath.IsAbs(name) || name == ".." || strings.HasPrefix(name, "../") || filepath.Clean(name) != name {
		return "", fmt.Errorf("unsafe hook path %q", name)
	}
	path := filepath.Join(root, name)
	for p := path; ; p = filepath.Dir(p) {
		s, e := os.Lstat(p)
		if e == nil && s.Mode()&os.ModeSymlink != 0 {
			return "", fmt.Errorf("hook path contains symlink: %s", p)
		}
		if e != nil && !os.IsNotExist(e) {
			return "", e
		}
		if p == filepath.Dir(p) {
			break
		}
	}
	return path, nil
}
func hookRead(path string) (hookFile, error) {
	if _, e := hookPath(filepath.Dir(path), filepath.Base(path)); e != nil {
		return hookFile{}, e
	}
	s, e := os.Lstat(path)
	if os.IsNotExist(e) {
		return hookFile{}, nil
	}
	if e != nil {
		return hookFile{}, e
	}
	if !s.Mode().IsRegular() {
		return hookFile{}, fmt.Errorf("expected regular hook input: %s", path)
	}
	b, e := os.ReadFile(path)
	return hookFile{b, s.Mode().Perm(), true}, e
}
func hookEqual(a, b hookFile) bool {
	return a.Exists == b.Exists && a.Mode == b.Mode && bytes.Equal(a.Body, b.Body)
}
func hookWrite(path string, b []byte, mode os.FileMode) error {
	if _, e := hookPath(filepath.Dir(path), filepath.Base(path)); e != nil {
		return e
	}
	f, e := os.CreateTemp(filepath.Dir(path), ".chainman-write-*")
	if e != nil {
		return e
	}
	defer os.Remove(f.Name())
	defer f.Close()
	if e = f.Chmod(mode); e != nil {
		return e
	}
	if _, e = f.Write(b); e != nil {
		return e
	}
	if e = f.Sync(); e != nil {
		return e
	}
	if e = f.Close(); e != nil {
		return e
	}
	return os.Rename(f.Name(), path)
}
func hookRestore(path string, f hookFile) error {
	if f.Exists {
		return hookWrite(path, f.Body, f.Mode)
	}
	e := os.Remove(path)
	if os.IsNotExist(e) {
		return nil
	}
	return e
}
func hookEnv(clean bool) []string {
	env := []string{}
	for _, v := range os.Environ() {
		k, _, _ := strings.Cut(v, "=")
		if strings.HasPrefix(k, "GIT_") {
			if clean || !(strings.HasPrefix(k, "GIT_CONFIG_") || k == "GIT_ATTR_NOSYSTEM") {
				continue
			}
		}
		env = append(env, v)
	}
	env = append(env, "GIT_NO_REPLACE_OBJECTS=1", "GIT_OPTIONAL_LOCKS=0")
	if clean {
		env = append(env, "GIT_CONFIG_GLOBAL=/dev/null", "GIT_CONFIG_SYSTEM=/dev/null", "GIT_CONFIG_NOSYSTEM=1")
	}
	return env
}
func (p HookPlan) git(root, index string, input []byte, policy bool, args ...string) ([]byte, error) {
	argv := []string{"--literal-pathspecs", "-C", root, "-c", "core.fsmonitor=false", "-c", "gc.auto=0"}
	// Configuration queries must see the real selected hooksPath.
	if !policy {
		argv = append(argv, "-c", "core.hooksPath=/dev/null")
	}
	c := exec.Command(p.Git, append(argv, args...)...)
	c.Env = hookEnv(!policy)
	if index != "" {
		c.Env = append(c.Env, "GIT_INDEX_FILE="+index)
	}
	c.Stdin = bytes.NewReader(input)
	var errout bytes.Buffer
	c.Stderr = &errout
	b, e := c.Output()
	if e != nil {
		if errout.Len() > 0 {
			fmt.Fprint(os.Stderr, errout.String())
		}
		return b, fmt.Errorf("git %s: %w: %s", strings.Join(args, " "), e, errout.String())
	}
	return b, nil
}
func (p HookPlan) query(args ...string) (string, error) {
	b, e := p.git(p.Root, "", nil, false, args...)
	return strings.TrimSuffix(string(b), "\n"), e
}
func hookAbsent(e error) bool {
	var status *exec.ExitError
	return errorsAsExit(e, &status) && status.ExitCode() == 1
}
func errorsAsExit(e error, target **exec.ExitError) bool {
	for e != nil {
		if x, ok := e.(*exec.ExitError); ok {
			*target = x
			return true
		}
		u, ok := e.(interface{ Unwrap() error })
		if !ok {
			return false
		}
		e = u.Unwrap()
	}
	return false
}
func (p HookPlan) worker(phase, dir string, args ...string) error {
	var e error
	root := p.Root
	if phase == "format" {
		root = filepath.Join(dir, "snapshot")
	}
	authority := filepath.Join(dir, "authority")
	if e = os.MkdirAll(authority, 0700); e != nil {
		return e
	}
	for name, body := range p.Authority {
		path, e := hookPath(authority, name)
		if e != nil {
			return e
		}
		if e = os.MkdirAll(filepath.Dir(path), 0700); e != nil {
			return e
		}
		if e = os.WriteFile(path, body, 0600); e != nil {
			return e
		}
	}
	if e = os.WriteFile(filepath.Join(authority, "authority-root"), []byte(root+"\n"), 0600); e != nil {
		return e
	}
	inventory := []byte{}
	if phase == "format" {
		inventory = []byte(".git\n")
	}
	if e = os.WriteFile(filepath.Join(authority, "git-directories"), inventory, 0600); e != nil {
		return e
	}
	mounts := filepath.Join(dir, "mounts")
	result := filepath.Join(dir, "result")
	if e = os.MkdirAll(result, 0700); e != nil {
		return e
	}
	input := filepath.Join(dir, "input.json")
	options := "--mount\ntype=bind,src=" + input + ",dst=" + input + ",readonly\n--mount\ntype=bind,src=" + result + ",dst=" + result + "\n"
	if phase == "scan-check" {
		blobs := filepath.Join(dir, "blobs")
		options += "--mount\ntype=bind,src=" + blobs + ",dst=" + blobs + ",readonly\n"
	}
	// Mount only phase data, never original index/recovery files or host binaries.
	if e = os.WriteFile(mounts, []byte(options), 0600); e != nil {
		return e
	}

	argv := append([]string{p.Launcher, "_hook-worker", phase, dir}, args...)
	c := exec.Command("sh", argv...)
	c.Dir = root
	c.Env = hookWorkerEnv()
	c.Env = append(c.Env, "CHAINMAN_PROJECT_ROOT="+root, "CHAINMAN_ENTRY_AUTHORITY="+authority, "CHAINMAN_CONTAINER_OPTIONS_FILE="+mounts)
	c.Stdin = os.Stdin
	c.Stdout = os.Stdout
	c.Stderr = os.Stderr
	return hookRun(c)
}
func hookOperation(root string) (func(), error) {
	base, e := hookPath(root, ".cache/toolchain")
	if e != nil {
		return nil, e
	}
	if e = os.MkdirAll(filepath.Join(base, "operations"), 0700); e != nil {
		return nil, e
	}
	held := []*os.File{}
	done := func() {
		for i := len(held) - 1; i >= 0; i-- {
			held[i].Close()
		}
	}
	take := func(name string, flags int) (*os.File, error) {
		path, e := hookPath(base, name)
		if e != nil {
			return nil, e
		}
		f, e := os.OpenFile(path, os.O_RDWR|os.O_CREATE|syscall.O_NOFOLLOW, 0600)
		if e != nil {
			return nil, e
		}
		if e = syscall.Flock(int(f.Fd()), flags|syscall.LOCK_NB); e != nil {
			f.Close()
			return nil, fmt.Errorf("another managed operation is active; retry when it finishes: %w", e)
		}
		held = append(held, f)
		return f, nil
	}
	if _, e = take("operation.lock", syscall.LOCK_SH); e != nil {
		done()
		return nil, e
	}
	gate, e := take("operation-admission.lock", syscall.LOCK_EX)
	if e != nil {
		done()
		return nil, e
	}
	writer, e := take("writer.lock", syscall.LOCK_EX)
	if e != nil {
		done()
		return nil, e
	}
	f, e := os.CreateTemp(filepath.Join(base, "operations"), "hook-")
	if e != nil {
		done()
		return nil, e
	}
	held = append(held, f)
	if e = syscall.Flock(int(f.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); e != nil {
		done()
		return nil, e
	}
	writer.Close()
	gate.Close()
	return func() { done(); os.Remove(f.Name()) }, nil
}
func hookAction(args []string) int {
	if len(args) < 2 {
		return 2
	}
	var p HookPlan
	if e := readJSON(args[0], &p); e != nil {
		return exitCode(e)
	}
	if !filepath.IsAbs(p.Root) || !filepath.IsAbs(p.Git) || !filepath.IsAbs(p.Launcher) || filepath.Dir(args[0]) != p.Directory {
		return exitCode(fmt.Errorf("invalid native hook plan"))
	}
	switch args[1] {
	case "status", "check-install", "install", "uninstall", "config", "format-staged":
		if len(args) != 2 {
			fmt.Fprintln(os.Stderr, "This hook command accepts no additional arguments")
			return 2
		}
	}
	var e error
	switch args[1] {
	case "status":
		var s map[string]any
		s, e = p.hookStatus()
		if e == nil {
			e = json.NewEncoder(os.Stdout).Encode(s)
		}
	case "check-install":
		if p.Enabled {
			e = p.hookCheck()
			if e == nil {
				e = p.hookValidate()
			}
		}
	case "install":
		if p.Enabled {
			e = p.hookInstall(false)
		}
	case "uninstall":
		e = p.hookInstall(true)
	case "format-staged":
		e = p.hookFormat()
	case "trojan-source":
		e = p.hookScan(args[2:])
	case "task":
		if len(args) < 3 {
			return 2
		}
		c := exec.Command("sh", append([]string{p.Launcher, "run"}, args[2:]...)...)
		c.Env = hookEnv(true)
		c.Env = append(c.Env, "CHAINMAN_PROJECT_ROOT="+p.Root)
		c.Dir = p.Root
		c.Stdin = os.Stdin
		c.Stdin = os.Stdin
		c.Stdout = os.Stdout
		c.Stderr = os.Stderr
		e = hookRun(c)
	case "run":
		if len(args) < 3 || (args[2] != "pre-commit" && args[2] != "pre-push") || (args[2] == "pre-commit" && len(args) != 3) {
			fmt.Fprintln(os.Stderr, "Use hooks run pre-commit or hooks run pre-push REMOTE URL")
			return 2
		}
		fallthrough
	case "config":
		if !p.Enabled {
			return exitCode(fmt.Errorf("declare [hooks] enabled=true"))
		}
		a := []string{"dump"}
		if args[1] == "run" {
			a = append([]string{"run", "--no-auto-install"}, args[2:]...)
		}
		if args[1] == "run" && args[2] == "pre-push" {
			if len(args) != 5 {
				return 2
			}
			var b []byte
			b, e = io.ReadAll(os.Stdin)
			if e != nil {
				break
			}
			e = hookWrite(filepath.Join(p.Directory, "input"), b, 0600)
			if e != nil {
				break
			}
		}
		c := exec.Command(p.Lefthook, a...)
		c.Dir = p.Root
		c.Env = os.Environ()
		exe, x := os.Executable()
		if x != nil {
			e = x
			break
		}
		c.Env = append(c.Env, "LEFTHOOK_CONFIG="+p.Config, "CHAINMAN_HOOK_ENTRY="+filepath.Join(filepath.Dir(p.Launcher), "hook-task.sh"), "CHAINMAN_HOOK_HELPER="+exe, "CHAINMAN_HOOK_PLAN="+args[0])
		if args[1] == "run" && args[2] == "pre-push" {
			c.Env = append(c.Env, "CHAINMAN_HOOK_INPUT="+filepath.Join(p.Directory, "input"), "CHAINMAN_HOOK_REMOTE_NAME="+args[3], "CHAINMAN_HOOK_REMOTE_URL="+args[4])
		}
		c.Stdin = os.Stdin
		c.Stdout = os.Stdout
		c.Stderr = os.Stderr
		e = hookRun(c)
	default:
		return 2
	}
	return exitCode(e)
}

// Each finite child owns a process group, while this parent retains repository
// leases until that child has stopped. No background hook supervisor remains.
func hookRun(c *exec.Cmd) error {
	c.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	signals := make(chan os.Signal, 1)
	signal.Notify(signals, syscall.SIGINT, syscall.SIGTERM, syscall.SIGHUP)
	defer signal.Stop(signals)
	if e := c.Start(); e != nil {
		return e
	}
	done := make(chan error, 1)
	go func() { done <- c.Wait() }()
	select {
	case e := <-done:
		return e
	case sig := <-signals:
		_ = syscall.Kill(-c.Process.Pid, sig.(syscall.Signal))
		select {
		case <-done:
			return &startupInterrupted{sig.(syscall.Signal)}
		case <-time.After(8 * time.Second):
			_ = syscall.Kill(-c.Process.Pid, syscall.SIGKILL)
			<-done
			return &startupInterrupted{sig.(syscall.Signal)}
		}
	}
}

func (p HookPlan) hookValidate() error {
	c := exec.Command(p.Lefthook, "validate")
	c.Dir = p.Root
	c.Env = append(os.Environ(), "LEFTHOOK_CONFIG="+p.Config)
	c.Stdout = os.Stderr
	c.Stderr = os.Stderr
	return hookRun(c)
}

func hookWorkerEnv() []string {
	env := []string{}
	for _, entry := range hookEnv(true) {
		name, _, _ := strings.Cut(entry, "=")
		switch name {
		case "CHAINMAN_WORKSPACE_TRANSACTION_ROOT", "CHAINMAN_ROOT", "CHAINMAN_PROJECT_ROOT", "CHAINMAN_ENTRY_AUTHORITY", "CHAINMAN_ACTIVE_PROFILE", "CHAINMAN_ACTIVE_FINGERPRINT", "CHAINMAN_HOOK_INPUT", "CHAINMAN_HOOK_PLAN", "CHAINMAN_HOOK_HELPER", "CHAINMAN_HOOK_ENTRY":
			continue
		}
		if strings.HasPrefix(name, "TOOLCHAIN_OPERATION_") {
			continue
		}
		env = append(env, entry)
	}
	return env
}

// Callers bound each batch; blobs are read literally without checkout filters.
func (p HookPlan) blobBodies(oids []string) ([][]byte, error) {
	objects, e := p.git(p.Root, "", []byte(strings.Join(oids, "\n")+"\n"), false, "cat-file", "--batch")
	if e != nil {
		return nil, e
	}
	bodies := make([][]byte, 0, len(oids))
	offset := 0
	for _, oid := range oids {
		newline := bytes.IndexByte(objects[offset:], '\n')
		if newline < 0 {
			return nil, fmt.Errorf("invalid Git blob batch")
		}
		newline += offset
		fields := strings.Fields(string(objects[offset:newline]))
		if len(fields) != 3 || fields[0] != oid || fields[1] != "blob" {
			return nil, fmt.Errorf("missing Git blob %s", oid)
		}
		size, e := strconv.Atoi(fields[2])
		if e != nil || size < 0 || size > len(objects)-newline-2 {
			return nil, fmt.Errorf("invalid Git blob size")
		}
		bodies = append(bodies, objects[newline+1:newline+1+size])
		offset = newline + size + 2
	}
	return bodies, nil
}
