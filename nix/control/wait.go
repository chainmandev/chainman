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
		selected, e := ordered(saved, p.Requested)
		if e != nil {
			result <- e
			return
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
			unlock, e := watchGates(saved, selected)
			if e != nil {
				result <- e
				return
			}
			ps, e := states(saved)
			unlock()
			if e != nil {
				result <- fmt.Errorf("service controller lost: %w", e)
				return
			}
			wanted := map[string]bool{}
			for _, name := range selected {
				wanted[name] = true
			}
			for _, process := range ps {
				if wanted[process.Name] && (process.Status == "Error" || process.Status == "Completed" || process.Status == "Skipped") {
					result <- fmt.Errorf("service %s ended: %s", process.Name, process.Status)
					return
				}
			}
		}
	}()
	return result
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
