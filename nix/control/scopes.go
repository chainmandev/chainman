package main

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"syscall"
)

// Repository resources borrow the root task's existing lifetime witness instead
// of introducing another supervisor, heartbeat, or task process. A claim is
// recorded only after its parent receipt and resource intent have been saved.
type LeaseRef struct {
	State string `json:"state"`
	File  string `json:"file"`
}

func parentAlive(p Plan, ref LeaseRef) (bool, error) {
	if ref.State == p.State || filepath.Dir(ref.State) != filepath.Dir(p.State) || filepath.Base(ref.File) != ref.File || !strings.HasSuffix(ref.File, ".lease") {
		return false, fmt.Errorf("invalid resource parent lease")
	}
	if e := private(ref.State); e != nil {
		return false, e
	}
	file, e := os.OpenFile(filepath.Join(ref.State, ref.File), os.O_RDWR|syscall.O_NOFOLLOW, 0)
	if os.IsNotExist(e) {
		return false, nil
	}
	if e != nil {
		return false, e
	}
	defer file.Close()
	// A locked parent is live before its child publishes the exec identity. Never
	// wait on a parent gate: scope acquisition always locks parent then resource.
	e = syscall.Flock(int(file.Fd()), syscall.LOCK_EX|syscall.LOCK_NB)
	if errors.Is(e, syscall.EWOULDBLOCK) {
		return true, nil
	}
	if e != nil {
		return false, e
	}
	var l Lease
	if e = readJSON(filepath.Join(ref.State, ref.File), &l); os.IsNotExist(e) {
		return false, nil
	}
	if e != nil {
		return false, e
	}
	if l.Parent != nil {
		return false, fmt.Errorf("nested resource parent lease")
	}
	present, e := leaseTaskAlive(filepath.Join(ref.State, ref.File), l)
	if e != nil {
		return false, e
	}
	if l.Persistent || present {
		return true, nil
	}
	if l.Container != nil {
		info, e := inspectContainer(l.Container)
		return info != nil && info.Running, e
	}
	return false, nil
}

func hasLeases(p Plan) bool {
	files, _ := filepath.Glob(filepath.Join(p.State, "*.lease"))
	return len(files) > 0
}

func mergeResources(old, fresh []Plan) []Plan {
	for _, resource := range fresh {
		found := false
		for _, existing := range old {
			if existing.State == resource.State {
				found = true
			}
		}
		if !found {
			old = append(old, resource)
		}
	}
	return old
}

// Call with the worktree gate held. Resource cleanup never takes a parent gate;
// it observes a parent's locked descriptor or saved kernel/container identity.
func pruneResources(p Plan) error {
	// Networks are acquired before data services and released after endpoints.
	for index := len(p.Resources) - 1; index >= 0; index-- {
		resource := p.Resources[index]
		if filepath.Dir(resource.State) != filepath.Dir(p.State) || resource.State == p.State {
			return fmt.Errorf("invalid saved resource scope")
		}
		if _, e := os.Stat(resource.State); os.IsNotExist(e) {
			continue
		}
		if e := private(resource.State); e != nil {
			return e
		}
		gate, e := locked(filepath.Join(resource.State, "gate"), false)
		if e != nil {
			return e
		}
		var saved Plan
		e = readJSON(filepath.Join(resource.State, "plan.json"), &saved)
		if os.IsNotExist(e) {
			gate.Close()
			continue
		}
		if e == nil && (saved.State != resource.State || len(saved.Resources) > 0) {
			e = fmt.Errorf("invalid saved repository scope")
		}
		if e == nil {
			e = releaseUnused(saved, false)
		}
		gate.Close()
		if e != nil {
			return e
		}
	}
	return nil
}
