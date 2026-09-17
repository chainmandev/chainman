package main

import (
	"context"
	"fmt"
	"io"
	"os"
	"os/signal"
	"path/filepath"
	"syscall"
	"time"
)

const logHistoryBytes = 64 * 1024

// A viewer holds only read descriptors: it never acquires or releases services.
type logCursor struct {
	path   string
	file   *os.File
	offset int64
}

func (c *logCursor) close() {
	if c.file != nil {
		c.file.Close()
		c.file = nil
	}
}

func (c *logCursor) copy(w io.Writer) error {
	st, err := os.Lstat(c.path)
	if os.IsNotExist(err) {
		return nil
	}
	if err != nil {
		return err
	}
	if !st.Mode().IsRegular() {
		return fmt.Errorf("service log must be a regular file: %s", c.path)
	}
	if c.file != nil {
		old, e := c.file.Stat()
		if e != nil {
			return e
		}
		if !os.SameFile(st, old) {
			// Drain the old inode before following its replacement.
			if _, e = io.CopyN(w, c.file, logHistoryBytes); e != nil && e != io.EOF {
				return e
			}
			c.close()
		} else if st.Size() < c.offset {
			c.offset = 0
			if _, e = c.file.Seek(0, io.SeekStart); e != nil {
				return e
			}
		}
	}
	if c.file == nil {
		c.file, err = os.OpenFile(c.path, os.O_RDONLY|syscall.O_NOFOLLOW|syscall.O_NONBLOCK, 0)
		if os.IsNotExist(err) {
			return nil
		} // A rotation can remove the name between stat and open.
		if err != nil {
			return err
		}
		st, err = c.file.Stat()
		if err != nil {
			return err
		}
		if !st.Mode().IsRegular() {
			return fmt.Errorf("service log must be a regular file: %s", c.path)
		}
		c.offset = max(0, st.Size()-logHistoryBytes)
		if _, err = c.file.Seek(c.offset, io.SeekStart); err != nil {
			return err
		}
	}
	// Bounded reads keep cancellation and other resource scopes responsive.
	n, err := io.CopyN(w, c.file, logHistoryBytes)
	c.offset += n
	if err == io.EOF {
		return nil
	}
	return err
}

func logCursors(p Plan) ([]*logCursor, error) {
	states := []string{p.State}
	for _, resource := range p.Resources {
		if filepath.Dir(resource.State) != filepath.Dir(p.State) || resource.State == p.State {
			return nil, fmt.Errorf("invalid saved resource scope")
		}
		if resource.Bridge == nil {
			states = append(states, resource.State)
		}
	}
	var cursors []*logCursor
	seen := map[string]bool{}
	for _, state := range states {
		if seen[state] {
			continue
		}
		seen[state] = true
		if e := private(state); e != nil {
			return nil, e
		}
		cursors = append(cursors, &logCursor{path: filepath.Join(state, "services.log")})
	}
	return cursors, nil
}

func streamLogs(ctx context.Context, p Plan, w io.Writer, follow bool) error {
	cursors, err := logCursors(p)
	if err != nil {
		return err
	}
	defer func() {
		for _, c := range cursors {
			c.close()
		}
	}()
	tick := time.NewTicker(100 * time.Millisecond)
	defer tick.Stop()
	for {
		for _, c := range cursors {
			if err = c.copy(w); err != nil {
				return err
			}
		}
		if !follow {
			return nil
		}
		select {
		case <-ctx.Done():
			return nil
		case <-tick.C:
		}
	}
}

func serviceLogs(state string, follow bool) int {
	if e := private(state); e != nil {
		return exitCode(e)
	}
	var p Plan
	if e := readJSON(filepath.Join(state, "plan.json"), &p); os.IsNotExist(e) {
		fmt.Fprintln(os.Stderr, "No saved services for this worktree and mode.")
		return 0
	} else if e != nil {
		return exitCode(e)
	}
	if p.State != state {
		return exitCode(fmt.Errorf("saved service scope does not match requested state"))
	}
	ctx, cancel := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM, syscall.SIGHUP)
	defer cancel()
	done := make(chan error, 1)
	go func() { done <- streamLogs(ctx, p, os.Stdout, follow) }()
	select {
	case err := <-done:
		return exitCode(err)
	case <-ctx.Done():
		// Exit even if a consumer stopped draining our stdout pipe. This command
		// owns no services and main's process exit closes its read descriptors.
		return 0
	}
}
