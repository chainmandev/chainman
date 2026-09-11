package main

import (
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"time"
)

// Watchexec owns filesystem observation, debounce and queued builds. Process
// Compose owns service replacement. This adapter only commits successful builds.
type Watch struct {
	Build     Command    `json:"build"`
	Paths     []string   `json:"paths"`
	Ignore    []string   `json:"ignore"`
	Debounce  int        `json:"debounce_ms"`
	Startup   int        `json:"startup_seconds"`
	Container *Container `json:"container,omitempty"`
}

const watchSuffix = "-chainman-watch"

func expandWatches(p *Plan, self string) error {
	for name, s := range p.Services {
		if strings.HasSuffix(name, watchSuffix) {
			return fmt.Errorf("reserved service name: %s", name)
		}
		if s.Watch == nil {
			continue
		}
		w := s.Watch
		if !filepath.IsAbs(p.Watcher) || len(w.Paths) == 0 || w.Debounce < 1 || w.Debounce > 60000 || w.Startup < 1 || w.Startup > 600 {
			return fmt.Errorf("invalid watch declaration")
		}
		if _, e := child(w.Build); e != nil {
			return e
		}
	}
	additional := map[string]Service{}
	for name, s := range p.Services {
		if s.Watch == nil {
			continue
		}
		w := s.Watch
		argv := []string{p.Watcher, "--shell=none", "--wrap-process=none", "--on-busy-update=queue", "--debounce=" + strconv.Itoa(w.Debounce) + "ms", "--project-origin", p.Root}
		for _, path := range w.Paths {
			argv = append(argv, "--watch", path)
		}
		for _, pattern := range w.Ignore {
			argv = append(argv, "--ignore", pattern)
		}
		argv = append(argv, "--", self, "build", p.State, name)
		hidden := name + watchSuffix
		additional[hidden] = Service{
			Command:      Command{Argv: argv, Directory: p.Root},
			Dependencies: append([]string{}, s.Dependencies...),
			Restart:      "no", Shutdown: s.Shutdown, Container: w.Container,
			Readiness: &Probe{Command: Command{Argv: []string{self, "built", p.State, name}, Directory: p.Root}, Period: 1, Timeout: 1, Failures: w.Startup},
		}
		s.Dependencies = append(s.Dependencies, hidden)
		p.Services[name] = s
	}
	for name, s := range additional {
		p.Services[name] = s
	}
	return nil
}

func watchAction(action, state, name string) int {
	marker := filepath.Join(state, name+".built.json")
	if action == "built" {
		var result map[string]string
		if e := readJSON(marker, &result); e != nil {
			return 1
		}
		return 0
	}
	var p Plan
	if e := readJSON(filepath.Join(state, "plan.json"), &p); e != nil {
		return exitCode(e)
	}
	s, ok := p.Services[name]
	if p.State != state || !ok || s.Watch == nil {
		return 2
	}
	w := s.Watch
	if _, e := os.Stat(filepath.Join(state, name+".watch-stopping")); e == nil {
		return 0
	} else if !os.IsNotExist(e) {
		return exitCode(e)
	}
	var previous map[string]string
	hadSuccess := readJSON(marker, &previous) == nil
	if e := checkEngine(w.Container); e != nil {
		return exitCode(e)
	}
	// Clean an interrupted previous build by its saved label and immutable ID.
	if e := stopContainer(w.Container, s.Shutdown); e != nil {
		return exitCode(e)
	}
	cmd, e := child(w.Build)
	if e != nil {
		return exitCode(e)
	}
	e = cmd.Run()
	if cleanup := stopContainer(w.Container, s.Shutdown); cleanup != nil {
		return exitCode(cleanup)
	}
	result := map[string]string{"finished": time.Now().UTC().Format(time.RFC3339Nano)}
	if e != nil {
		result["error"] = e.Error()
		_ = atomic(filepath.Join(state, name+".build-result.json"), result)
		fmt.Fprintln(os.Stderr, "build failed; retaining the last successful service:", name)
		return exitCode(e)
	}
	gate, e := locked(filepath.Join(state, name+".build-gate"), false)
	if e != nil {
		return exitCode(e)
	}
	defer gate.Close()
	if _, e := os.Stat(filepath.Join(state, name+".watch-stopping")); e == nil {
		return 0
	} else if !os.IsNotExist(e) {
		return exitCode(e)
	}
	if e = atomic(filepath.Join(state, name+".build-result.json"), result); e != nil {
		return exitCode(e)
	}
	if e = atomic(marker, result); e != nil {
		return exitCode(e)
	}
	// Publishing first-build readiness may immediately admit the dependent process.
	// That newly started process must not be restarted by this same first build.
	if !hadSuccess {
		return 0
	}
	ps, e := states(p)
	if e != nil {
		return exitCode(e)
	}
	for _, process := range ps {
		if process.Name == name && process.Running {
			_, e = backend(p, "process", "restart", name)
			return exitCode(e)
		}
	}
	return 0
}

func watchStopping(p Plan, name string, stopping bool) error {
	gate, e := locked(filepath.Join(p.State, name+".build-gate"), false)
	if e != nil {
		return e
	}
	defer gate.Close()
	path := filepath.Join(p.State, name+".watch-stopping")
	if stopping {
		return atomic(path, true)
	}
	if e = os.Remove(path); os.IsNotExist(e) {
		return nil
	}
	return e
}

// Serialize admission/inspection with a build's short commit-and-restart phase.
// Build execution itself never holds these locks, so stop remains bounded.
func watchGates(p Plan, names []string) (func(), error) {
	selected := append([]string{}, names...)
	sort.Strings(selected)
	files := []*os.File{}
	closeAll := func() {
		for _, file := range files {
			file.Close()
		}
	}
	for _, name := range selected {
		if p.Services[name].Watch == nil {
			continue
		}
		file, e := locked(filepath.Join(p.State, name+".build-gate"), false)
		if e != nil {
			closeAll()
			return nil, e
		}
		files = append(files, file)
	}
	return closeAll, nil
}

func startMissing(p Plan, selected []string, used map[string]bool) error {
	for _, name := range selected {
		if !used[name] {
			if e := serviceStopping(p, name, false); e != nil {
				return e
			}
		}
		if !used[name] && p.Services[name].Watch != nil {
			if e := watchStopping(p, name, false); e != nil {
				return e
			}
		}
	}
	unlock, e := watchGates(p, selected)
	if e != nil {
		return e
	}
	defer unlock()
	ps, e := states(p)
	if e != nil {
		return e
	}
	running := map[string]bool{}
	for _, s := range ps {
		running[s.Name] = s.Running
	}
	for _, name := range selected {
		if used[name] || running[name] {
			continue
		}
		if strings.HasSuffix(name, watchSuffix) {
			if e = os.Remove(filepath.Join(p.State, strings.TrimSuffix(name, watchSuffix)+".built.json")); e != nil && !os.IsNotExist(e) {
				return e
			}
		}
		if _, e = backend(p, "process", "start", name); e != nil {
			return e
		}
	}
	return nil
}
