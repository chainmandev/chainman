package main

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestMonitorPerPoolDeadlinePolicy(t *testing.T) {
	for _, name := range []string{"transient", "persistent", "other-error", "service-ended", "stop", "cancel"} {
		t.Run(name, func(t *testing.T) {
			state := t.TempDir()
			pools := []Plan{{State: "pool-a"}, {State: "pool-b"}}
			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			calls := map[string]int{}
			explicit := errors.New("service healthy ended: Error")
			err := monitorServicePools(ctx, state, pools, time.Millisecond, func(p Plan) error {
				calls[p.State]++
				if p.State == "pool-b" {
					return nil
				}
				switch name {
				case "transient":
					// A successful observation resets this pool, allowing a later isolated deadline.
					if calls[p.State] == 1 || calls[p.State] == 3 {
						return context.DeadlineExceeded
					}
					if calls[p.State] == 4 {
						cancel()
					}
					return nil
				case "persistent":
					return context.DeadlineExceeded
				case "other-error", "service-ended":
					return explicit
				case "stop":
					if e := os.WriteFile(filepath.Join(state, "stop-request.json"), nil, 0600); e != nil {
						t.Fatal(e)
					}
					return context.DeadlineExceeded
				case "cancel":
					cancel()
					return context.DeadlineExceeded
				}
				panic("unknown fixture")
			})
			switch name {
			case "transient":
				if err != nil || calls["pool-a"] != 4 {
					t.Fatalf("recovery/reset failed: %v %v", err, calls)
				}
			case "persistent":
				if !errors.Is(err, context.DeadlineExceeded) || !strings.Contains(err.Error(), "pool-a failed two consecutive") || calls["pool-a"] != 2 || calls["pool-b"] != 1 {
					t.Fatalf("per-pool bounded failure lost: %v %v", err, calls)
				}
			case "other-error", "service-ended":
				if err != explicit || calls["pool-a"] != 1 {
					t.Fatalf("failure delayed: %v %v", err, calls)
				}
			case "stop":
				if !errors.Is(err, servicesStopped) {
					t.Fatalf("stop lost: %v", err)
				}
			case "cancel":
				if err != nil {
					t.Fatalf("cancel changed: %v", err)
				}
			}
		})
	}
}

func TestMonitorActualServiceOutcomesRemainImmediate(t *testing.T) {
	for _, status := range []string{"Error", "Completed", "Skipped", "Running", "query-error"} {
		t.Run(status, func(t *testing.T) {
			dir := t.TempDir()
			backend := filepath.Join(dir, "backend")
			script := "#!/bin/sh\nprintf '%s' '[{\"name\":\"healthy\",\"status\":\"" + status + "\"}]'\n"
			if status == "query-error" {
				script = "#!/bin/sh\nexit 7\n"
			}
			if err := os.WriteFile(backend, []byte(script), 0700); err != nil {
				t.Fatal(err)
			}
			p := Plan{Root: dir, State: dir, Backend: backend, Services: map[string]Service{"healthy": {}}, Requested: []string{"healthy"}}
			err := serviceOutcome(p, nil)
			if status == "Running" {
				if err != nil {
					t.Fatal(err)
				}
				return
			}
			if err == nil || errors.Is(err, context.DeadlineExceeded) {
				t.Fatalf("failure weakened: %v", err)
			}
			if status == "query-error" {
				if exitCode(err) != 7 {
					t.Fatalf("ordinary query exit changed: %v", err)
				}
			} else if !strings.Contains(err.Error(), "ended: "+status) {
				t.Fatal(err)
			}
		})
	}
}
