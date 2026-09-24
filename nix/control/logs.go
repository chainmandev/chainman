package main

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"os/signal"
	"path/filepath"
	"strings"
	"syscall"
	"time"
)

const logHistoryBytes = 64 * 1024

// A viewer holds only read descriptors: it never acquires or releases services.
type logCursor struct {
	path        string
	file        *os.File
	offset      int64
	initialized bool
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
		c.offset = 0
		if !c.initialized {
			c.offset = max(0, st.Size()-logHistoryBytes)
		}
		c.initialized = true
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
	return streamLogCursors(ctx, cursors, w, follow)
}

// Freeze the live-view boundary before acquiring services, including logs that
// do not exist yet. Startup output must neither be replayed nor lost in a race.
func liveLogCursors(p Plan) ([]*logCursor, error) {
	cursors, err := logCursors(p)
	if err != nil {
		return nil, err
	}
	for _, c := range cursors {
		if err := c.copy(io.Discard); err != nil {
			for _, open := range cursors {
				open.close()
			}
			return nil, err
		}
		c.initialized = true
	}
	return cursors, nil
}

// Process Compose's level denotes stdout/stderr, not application severity.
// Decode its envelope only; retain the application's complete message and pass
// historical plain-text logs and controller diagnostics through unchanged.
type logRenderer struct {
	destination io.Writer
	pending     []byte
}

func (r *logRenderer) line(line []byte) error {
	var record struct {
		Level   string `json:"level"`
		Process string `json:"process"`
		Time    string `json:"time"`
		Message string `json:"message"`
	}
	if json.Unmarshal(line, &record) == nil && validName.MatchString(record.Process) && (record.Level == "info" || record.Level == "error") {
		stream := "stdout"
		if record.Level == "error" {
			stream = "stderr"
		}
		_, err := fmt.Fprintf(r.destination, "%s [%s %s] %s\n", record.Time, record.Process, stream, record.Message)
		return err
	}
	_, err := r.destination.Write(line)
	return err
}

func (r *logRenderer) Write(data []byte) (int, error) {
	size := len(data)
	r.pending = append(r.pending, data...)
	for {
		i := bytes.IndexByte(r.pending, '\n')
		if i < 0 {
			break
		}
		if err := r.line(r.pending[:i+1]); err != nil {
			return 0, err
		}
		r.pending = r.pending[i+1:]
	}
	// Do not accumulate unbounded memory for an unfinished application line.
	if len(r.pending) >= logHistoryBytes {
		if _, err := r.destination.Write(r.pending); err != nil {
			return 0, err
		}
		r.pending = nil
	}
	return size, nil
}

func streamLogCursors(ctx context.Context, cursors []*logCursor, w io.Writer, follow bool) error {
	defer func() {
		for _, c := range cursors {
			c.close()
		}
	}()
	renderers := make([]*logRenderer, len(cursors))
	for i := range cursors {
		renderers[i] = &logRenderer{destination: w}
	}
	tick := time.NewTicker(100 * time.Millisecond)
	defer tick.Stop()
	for {
		for i, c := range cursors {
			if err := c.copy(renderers[i]); err != nil {
				return err
			}
		}
		if !follow {
			for _, r := range renderers {
				if _, err := w.Write(r.pending); err != nil {
					return err
				}
			}
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

// Finite tasks stay quiet on success. On failed acquisition, read only the end
// of output produced since admission; a noisy startup must not hide its error.
func (c *logCursor) latest(w io.Writer) error {
	if c.file != nil {
		st, err := c.file.Stat()
		if err != nil {
			return err
		}
		if st.Size() < c.offset {
			c.offset = 0
		}
		c.offset = max(c.offset, st.Size()-logHistoryBytes)
		if _, err = c.file.Seek(c.offset, io.SeekStart); err != nil {
			return err
		}
	}
	// copy handles replacement and truncation. For a newly created/replaced
	// file use its bounded tail, not its possibly enormous initial prefix.
	st, err := os.Lstat(c.path)
	if err == nil && c.file != nil {
		old, e := c.file.Stat()
		if e != nil {
			return e
		}
		if !os.SameFile(st, old) {
			if _, e = io.CopyN(w, c.file, logHistoryBytes); e != nil && e != io.EOF {
				return e
			}
			c.close()
		}
	}
	if c.file == nil {
		c.initialized = false
	}
	return c.copy(w)
}

func startupDiagnostics(cursors []*logCursor) {
	var message strings.Builder
	fmt.Fprintln(&message, "Service startup failed; recent output from this attempt:")
	for _, cursor := range cursors {
		fmt.Fprintln(&message, "Log: "+cursor.path)
		var tail diagnosticTail
		renderer := &logRenderer{destination: &tail}
		if err := cursor.latest(renderer); err != nil {
			fmt.Fprintln(&message, "  Log unavailable:", err)
			continue
		}
		_, _ = tail.Write(renderer.pending)
		excerpt := tail.excerpt()
		if excerpt == "" {
			fmt.Fprintln(&message, "  No new output captured.")
		} else {
			fmt.Fprintln(&message, excerpt)
		}
	}
	output := newDevelopmentOutput()
	output.write(message.String())
	output.close()
}
