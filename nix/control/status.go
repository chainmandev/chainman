package main

import (
	"context"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"syscall"
	"time"
)

// Observers never wait for ownership, create lease files, or reap abandoned
// clients. One deadline covers every external probe across all resource scopes.
const statusTimeout = 2 * time.Second

func observeLock(path string) (*os.File, error) {
	file, err := os.OpenFile(path, os.O_RDONLY|syscall.O_NOFOLLOW|syscall.O_NONBLOCK, 0)
	if err != nil {
		return nil, err
	}
	st, err := file.Stat()
	if err == nil && !st.Mode().IsRegular() {
		err = fmt.Errorf("not a regular state file: %s", path)
	}
	if err == nil {
		err = syscall.Flock(int(file.Fd()), syscall.LOCK_SH|syscall.LOCK_NB)
	}
	if err != nil {
		file.Close()
		return nil, err
	}
	return file, nil
}

func observedLease(ctx context.Context, state, path string, parent bool) (Lease, bool, error) {
	var lease Lease
	err := readJSON(path, &lease)
	if os.IsNotExist(err) {
		return lease, false, nil
	}
	if err != nil {
		return lease, false, err
	}
	if lease.Parent != nil {
		ref := lease.Parent
		if parent || ref.State == state || filepath.Dir(ref.State) != filepath.Dir(state) || filepath.Base(ref.File) != ref.File || !strings.HasSuffix(ref.File, ".lease") {
			return lease, false, fmt.Errorf("invalid resource parent lease")
		}
		if _, err := os.Stat(ref.State); err != nil {
			return lease, false, err
		}
		if err := existingPrivate(ref.State); err != nil {
			return lease, false, err
		}
		_, alive, err := observedLease(ctx, ref.State, filepath.Join(ref.State, ref.File), true)
		return lease, alive, err
	}
	file, err := observeLock(path)
	if errors.Is(err, syscall.EWOULDBLOCK) {
		return lease, true, nil
	}
	if os.IsNotExist(err) {
		return lease, false, nil
	}
	if err != nil {
		return lease, false, err
	}
	defer file.Close()
	if lease.Persistent {
		return lease, true, nil
	}
	alive, err := leaseTaskAlive(path, lease)
	if err != nil || alive || lease.Container == nil {
		return lease, alive, err
	}
	info, err := inspectContainerContext(ctx, lease.Container)
	return lease, info != nil && info.Running, err
}

func observedLeases(ctx context.Context, p Plan) (map[string]bool, int, error) {
	used := map[string]bool{}
	entries, err := os.ReadDir(p.State)
	if err != nil {
		return nil, 0, err
	}
	clients := 0
	for _, entry := range entries {
		if err := ctx.Err(); err != nil {
			return nil, 0, err
		}
		if !strings.HasSuffix(entry.Name(), ".lease") {
			continue
		}
		lease, live, err := observedLease(ctx, p.State, filepath.Join(p.State, entry.Name()), false)
		if err != nil {
			return nil, 0, err
		}
		if live {
			clients++
			for _, name := range lease.Services {
				used[name] = true
			}
		}
	}
	return used, clients, nil
}

func incompleteStatus(value map[string]any, detail string) {
	value["complete"] = false
	value["inspection_errors"] = append(value["inspection_errors"].([]string), detail)
}

func scopeStatus(ctx context.Context, state string, resource bool) (map[string]any, []Plan, error) {
	value := map[string]any{
		"state": state, "running": nil, "leases": nil, "services": nil,
		"resources": []map[string]any{}, "log": filepath.Join(state, "services.log"),
		"recovery_required": nil, "complete": true, "busy": false, "inspection_errors": []string{},
	}
	// Validate existing components and permissions without creating any state.
	if _, err := os.Lstat(state); os.IsNotExist(err) {
		value["running"], value["services"], value["leases"], value["recovery_required"] = false, []Process{}, map[string]bool{}, false
		return value, nil, nil
	} else if err != nil {
		return nil, nil, err
	}
	if err := existingPrivate(state); err != nil {
		return nil, nil, err
	}
	gate, err := observeLock(filepath.Join(state, "gate"))
	if errors.Is(err, syscall.EWOULDBLOCK) {
		value["busy"] = true
		incompleteStatus(value, "scope is starting, stopping, or changing; observations may be incomplete")
	} else if err != nil && !os.IsNotExist(err) {
		return nil, nil, err
	}
	if gate != nil {
		defer gate.Close()
	}
	var p Plan
	if err := readJSON(filepath.Join(state, "plan.json"), &p); os.IsNotExist(err) {
		if value["busy"] != true {
			value["running"], value["services"], value["leases"], value["recovery_required"] = false, []Process{}, map[string]bool{}, false
		}
		return value, nil, nil
	} else if err != nil {
		return nil, nil, err
	}
	if p.State != state || (resource && len(p.Resources) > 0) {
		return nil, nil, fmt.Errorf("invalid saved service scope")
	}
	used, clients, leaseError := observedLeases(ctx, p)
	if leaseError != nil {
		incompleteStatus(value, "lease observation unavailable: "+leaseError.Error())
	} else {
		value["leases"] = used
	}
	if p.Bridge != nil {
		value["bridge"], value["clients"], value["network_id"] = p.Bridge.Name, nil, nil
		if leaseError == nil {
			value["clients"] = clients
		}
		current, err := inspectBridgeContext(ctx, p.Bridge)
		if err != nil {
			incompleteStatus(value, "bridge observation unavailable: "+err.Error())
		} else {
			id := ""
			if current != nil {
				id = current.ID
			}
			value["running"], value["network_id"] = current != nil, id
			if leaseError == nil && value["busy"] != true {
				value["recovery_required"] = current == nil && clients > 0
			}
		}
	} else {
		live := controller(p).alive()
		value["running"] = live
		ps, err := statesContext(ctx, p)
		if err != nil {
			if live || clients > 0 || value["busy"] == true || leaseError != nil {
				incompleteStatus(value, "service observation unavailable: "+err.Error())
			} else {
				value["services"] = []Process{}
			}
		} else {
			value["services"] = ps
		}
		if leaseError == nil && value["busy"] != true {
			value["recovery_required"] = err != nil && clients > 0
		}
	}
	return value, p.Resources, nil
}

func serviceStatus(state string, human bool) int {
	ctx, cancel := context.WithTimeout(context.Background(), statusTimeout)
	defer cancel()
	value, resources, err := scopeStatus(ctx, state, false)
	if err != nil {
		return exitCode(err)
	}
	rows := []map[string]any{}
	for _, resource := range resources {
		if len(resource.Resources) > 0 || resource.State == state || filepath.Dir(resource.State) != filepath.Dir(state) {
			return exitCode(fmt.Errorf("invalid saved repository scope"))
		}
		row, _, err := scopeStatus(ctx, resource.State, true)
		if err != nil {
			return exitCode(err)
		}
		rows = append(rows, row)
		if row["complete"] != true {
			value["complete"] = false
		}
	}
	value["resources"] = rows
	return printDevelopmentStatus(state, value, human)
}
