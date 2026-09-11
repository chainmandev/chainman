package main

import (
	"context"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"time"
)

var servicesStopped = errors.New("services explicitly stopped")

// A waiting client observes backend outcomes; it does not implement restarts or
// readiness probes. Setup leases remain in its ordinary project task process.
func monitorServices(ctx context.Context, p Plan) <-chan error {
	result := make(chan error, 1)
	go func() {
		var saved Plan
		if e := readJSON(filepath.Join(p.State, "plan.json"), &saved); e != nil {
			result <- e
			return
		}
		pools := []Plan{saved}
		pools[0].Requested = p.Requested
		for _, resource := range p.Resources {
			var pool Plan
			if e := readJSON(filepath.Join(resource.State, "plan.json"), &pool); e != nil {
				result <- e
				return
			}
			pool.Requested = resource.Requested
			pools = append(pools, pool)
		}
		ticker := time.NewTicker(time.Second)
		defer ticker.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-ticker.C:
			}
			if _, e := os.Stat(filepath.Join(p.State, "stop-request.json")); e == nil {
				result <- servicesStopped
				return
			}
			for _, pool := range pools {
				if e := serviceOutcome(pool); e != nil {
					result <- e
					return
				}
			}
		}
	}()
	return result
}

func serviceOutcome(p Plan) error {
	selected, e := ordered(p, p.Requested)
	if e != nil {
		return e
	}
	if len(selected) == 0 {
		return nil
	}
	unlock, e := watchGates(p, selected)
	if e != nil {
		return e
	}
	ps, e := states(p)
	unlock()
	if e != nil {
		return fmt.Errorf("service controller lost: %w", e)
	}
	wanted := map[string]bool{}
	for _, name := range selected {
		wanted[name] = true
	}
	for _, process := range ps {
		if wanted[process.Name] && (process.Status == "Error" || process.Status == "Completed" || process.Status == "Skipped") {
			return fmt.Errorf("service %s ended: %s", process.Name, process.Status)
		}
	}
	return nil
}

func cancelTask(cmd *exec.Cmd, done <-chan error, sig os.Signal, seconds int) error {
	if seconds < 1 {
		seconds = 10
	}
	_ = cmd.Process.Signal(sig)
	select {
	case e := <-done:
		return e
	case <-time.After(time.Duration(seconds+2) * time.Second):
		_ = cmd.Process.Kill()
		return <-done
	}
}
