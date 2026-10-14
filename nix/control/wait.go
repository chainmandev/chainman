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

// A service client observes backend outcomes; it does not implement restarts or
// readiness probes. Setup leases remain in its ordinary project task process.
func monitorServices(ctx context.Context, p Plan, development *developmentSession) <-chan error {
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
		err := monitorServicePools(ctx, p.State, pools, time.Second, func(pool Plan) error {
			return serviceOutcome(pool, development)
		})
		if ctx.Err() != nil {
			return
		}
		result <- err
	}()
	return result
}

// A single late read-only observation is inconclusive. Track consecutive
// deadlines separately for each pool; other pools cannot reset its evidence.
func monitorServicePools(ctx context.Context, state string, pools []Plan, interval time.Duration, outcome func(Plan) error) error {
	consecutive := make([]bool, len(pools))
	ticker := time.NewTicker(interval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return nil
		case <-ticker.C:
		}
		if _, err := os.Stat(filepath.Join(state, "stop-request.json")); err == nil {
			return servicesStopped
		}
		for index, pool := range pools {
			if ctx.Err() != nil {
				return nil
			}
			err := outcome(pool)
			if err == nil {
				consecutive[index] = false
				continue
			}
			if errors.Is(err, context.DeadlineExceeded) {
				if !consecutive[index] {
					consecutive[index] = true
					continue
				}
				return fmt.Errorf("service controller %s failed two consecutive status-query deadlines; check controller responsiveness and host resource pressure: %w", pool.State, err)
			}
			return err
		}
	}
}

func serviceOutcome(p Plan, development *developmentSession) error {
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
	development.services(p, ps, wanted)
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
