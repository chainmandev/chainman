package main

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"
)

// Called with the scope gate held, after stale claims have been reaped. Access
// attaches to the existing identity-backed lease, including parent-linked claims
// in repository pools; no separate lock owner or expiry mechanism is needed.
func checkServiceAccess(p Plan, selected []string) error {
	entries, err := os.ReadDir(p.State)
	if err != nil {
		return err
	}
	for _, entry := range entries {
		if !strings.HasSuffix(entry.Name(), ".lease") {
			continue
		}
		var lease Lease
		if err := readJSON(filepath.Join(p.State, entry.Name()), &lease); err != nil {
			return err
		}
		if !p.ExclusiveServices && !lease.ExclusiveServices {
			continue
		}
		for _, requested := range selected {
			for _, held := range lease.Services {
				if requested == held {
					return fmt.Errorf("service %s requires exclusive access; stop its other users first", requested)
				}
			}
		}
	}
	return nil
}
