package main

// Only the foreground caller reads consent. Private regular files work across
// container VM boundaries as well as host process groups; no network is exposed.
import (
	"context"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strings"
	"sync"
	"syscall"
	"time"

	"golang.org/x/sys/unix"
)

var consentID = regexp.MustCompile(`^[a-f0-9]{32}$`)

func consentWrite(root *os.Root, name, value string) error {
	if err := root.WriteFile(name+".new", []byte(value), 0600); err != nil {
		return err
	}
	return root.Rename(name+".new", name)
}

func consentRead(root *os.Root, name string, limit int64) (string, error) {
	// Nonblocking open lets validation reject a substituted FIFO before reading.
	f, err := root.OpenFile(name, os.O_RDONLY|syscall.O_NONBLOCK, 0)
	if err != nil {
		return "", err
	}
	defer f.Close()
	info, err := f.Stat()
	if err != nil || !info.Mode().IsRegular() || info.Size() > limit {
		return "", fmt.Errorf("invalid consent message")
	}
	b, err := io.ReadAll(io.LimitReader(f, limit))
	return string(b), err
}

type consentLease struct {
	value   string
	changed time.Time
}

func (l *consentLease) alive(root *os.Root, name string) bool {
	value, err := consentRead(root, name, 128)
	if err != nil || value == "" {
		return false
	}
	if value != l.value {
		l.value, l.changed = value, time.Now()
	}
	return time.Since(l.changed) < 10*time.Second
}

func hookPrompt() (func(), error) {
	nothing := func() {}
	policy, supplied := os.LookupEnv("CHAINMAN_SETUP")
	if (supplied && policy != "prompt") || os.Getenv("CHAINMAN_SETUP_CHANNEL") != "" {
		return nothing, nil
	}
	tty, err := os.OpenFile("/dev/tty", os.O_RDWR|syscall.O_NOCTTY|syscall.O_NONBLOCK, 0)
	if err == nil {
		var foreground int
		foreground, err = unix.IoctlGetInt(int(tty.Fd()), unix.TIOCGPGRP)
		if err == nil && foreground != syscall.Getpgrp() {
			err = fmt.Errorf("caller is not in the foreground")
		}
	}
	if err != nil {
		if tty != nil {
			tty.Close()
		}
		// A private lefthook PTY must not turn a noninteractive call into a prompt.
		os.Setenv("CHAINMAN_SETUP", "error")
		return func() {
			if supplied {
				os.Setenv("CHAINMAN_SETUP", policy)
			} else {
				os.Unsetenv("CHAINMAN_SETUP")
			}
		}, nil
	}
	directory, err := os.MkdirTemp("", "chainman-consent-")
	if err != nil {
		tty.Close()
		return nothing, err
	}
	physical, err := filepath.EvalSymlinks(directory)
	if err != nil {
		tty.Close()
		os.RemoveAll(directory)
		return nothing, err
	}
	directory = physical
	incoming, outgoing := (*os.Root)(nil), (*os.Root)(nil)
	release := func() {
		if incoming != nil {
			incoming.Close()
		}
		if outgoing != nil {
			outgoing.Close()
		}
		tty.Close()
		os.RemoveAll(directory)
	}
	for _, name := range []string{"incoming", "outgoing"} {
		if err = os.Mkdir(filepath.Join(directory, name), 0700); err != nil {
			release()
			return nothing, err
		}
	}
	incoming, err = os.OpenRoot(filepath.Join(directory, "incoming"))
	if err != nil {
		release()
		return nothing, err
	}
	outgoing, err = os.OpenRoot(filepath.Join(directory, "outgoing"))
	if err != nil {
		release()
		return nothing, err
	}
	if err = consentWrite(outgoing, "alive", "0"); err != nil {
		release()
		return nothing, err
	}
	os.Setenv("CHAINMAN_SETUP_CHANNEL", directory)
	ctx, cancel := context.WithCancel(context.Background())
	var serving sync.WaitGroup
	serving.Add(2)
	go func() {
		defer serving.Done()
		ticker := time.NewTicker(250 * time.Millisecond)
		defer ticker.Stop()
		for n := 1; ; n++ {
			select {
			case <-ctx.Done():
				return
			case <-ticker.C:
			}
			if consentWrite(outgoing, "alive", fmt.Sprint(n)) != nil {
				cancel()
				return
			}
		}
	}()
	go func() {
		defer serving.Done()
		ticker := time.NewTicker(100 * time.Millisecond)
		defer ticker.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-ticker.C:
			}
			entries, err := os.ReadDir(incoming.Name())
			if err != nil {
				cancel()
				return
			}
			for _, entry := range entries {
				if ctx.Err() != nil {
					return
				}
				if !entry.IsDir() || !consentID.MatchString(entry.Name()) {
					continue
				}
				if _, err := outgoing.Stat(entry.Name()); err == nil {
					continue
				}
				request, err := incoming.OpenRoot(entry.Name())
				if err != nil {
					continue
				}
				question, err := consentRead(request, "question", 1<<20)
				if !os.IsNotExist(err) {
					lease := &consentLease{}
					alive := func() bool { return lease.alive(request, "alive") }
					answer := "no\n"
					if err == nil && alive() {
						// Keep the question visible above lefthook's progress spinner.
						if _, err := fmt.Fprintln(tty, "\n"+question); err == nil && hookAnswer(ctx, tty, alive) {
							answer = "yes\n"
						}
					}
					if consentWrite(outgoing, entry.Name(), answer) != nil {
						cancel()
					}
				}
				request.Close()
			}
		}
	}()
	return func() { cancel(); serving.Wait(); release(); os.Unsetenv("CHAINMAN_SETUP_CHANNEL") }, nil
}

func hookAnswer(ctx context.Context, tty *os.File, alive func() bool) bool {
	fd := int(tty.Fd())
	if err := unix.SetNonblock(fd, true); err != nil {
		return false
	}
	// Darwin's poll does not support /dev/tty. A nonblocking read with a
	// bounded retry works for both platforms and remains cancellable.
	retry := time.NewTicker(100 * time.Millisecond)
	defer retry.Stop()
	answer := []byte{}
	for ctx.Err() == nil && alive() {
		var data [256]byte
		n, err := unix.Read(fd, data[:])
		if err == syscall.EAGAIN || err == syscall.EINTR {
			select {
			case <-ctx.Done():
				return false
			case <-retry.C:
			}
			continue
		}
		if err != nil || n == 0 {
			return false
		}
		answer = append(answer, data[:n]...)
		if len(answer) > 4096 {
			return false
		}
		if strings.Contains(string(answer), "\n") {
			switch strings.ToLower(strings.TrimSpace(string(answer))) {
			case "", "y", "yes":
				return true
			}
			return false
		}
	}
	return false
}

func consentAction(args []string) int {
	if len(args) == 0 {
		return 2
	}
	cleanup, err := hookPrompt()
	if err != nil {
		return exitCode(err)
	}
	defer cleanup()
	command := exec.Command(args[0], args[1:]...)
	command.Stdin, command.Stdout, command.Stderr = os.Stdin, os.Stdout, os.Stderr
	return exitCode(hookRun(command, 0))
}
