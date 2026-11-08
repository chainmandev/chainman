package main

import (
	"fmt"
	"path/filepath"
	"sort"
	"time"
)

func serviceEnded(process Process) bool {
	return process.Status == "Completed" || process.Status == "Error" || process.Status == "Skipped"
}

func serviceAvailable(service Service, process Process) bool {
	return !serviceEnded(process) && process.Running && (service.Readiness == nil || process.Ready == "Ready")
}

func serviceStartupBound(service Service) time.Duration {
	seconds := 1
	if r := service.Readiness; r != nil {
		// Optional startup uses the declared readiness window (at most ten
		// minutes), not a cleanup allowance multiplied by every probe. Probes
		// continue under Process Compose after this client starts degraded.
		seconds = r.Period*r.Failures + r.Timeout
	}
	return time.Duration(seconds) * time.Second
}

// Only an explicit continue policy relaxes unavailable service admission. A
// missing backend row or lost controller is still an ownership/control failure.
func startupServiceSample(p Plan, names []string, processes []Process, elapsed time.Duration) (pending bool, degraded []string, err error) {
	found := map[string]Process{}
	for _, process := range processes {
		found[process.Name] = process
	}
	for _, name := range names {
		process, ok := found[name]
		if !ok {
			return false, nil, fmt.Errorf("backend omitted service %s", name)
		}
		service := p.Services[name]
		if serviceAvailable(service, process) {
			continue
		}
		if service.FailurePolicy == "continue" && (serviceEnded(process) || elapsed >= serviceStartupBound(service)) {
			degraded = append(degraded, name)
			continue
		}
		if serviceEnded(process) {
			detail := process.Status
			if process.ExitCode != nil {
				detail += fmt.Sprintf(", exit code %d", *process.ExitCode)
			}
			return false, nil, fmt.Errorf("service %s exited before readiness (%s); log: %s", name, detail, filepath.Join(p.State, "services.log"))
		}
		pending = true
	}
	return pending, degraded, nil
}

// Status exposes current unavailable optional services independently of task
// success. Recovery naturally clears the list on the next observation.
func degradedServices(p Plan, processes []Process, selected map[string]bool) []string {
	names := []string{}
	for _, process := range processes {
		service, ok := p.Services[process.Name]
		if ok && selected[process.Name] && service.FailurePolicy == "continue" && !serviceAvailable(service, process) {
			names = append(names, process.Name)
		}
	}
	sort.Strings(names)
	return names
}
