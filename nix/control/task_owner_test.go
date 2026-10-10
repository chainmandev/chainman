package main

import (
	"os"
	"syscall"
	"testing"
	"time"

	"golang.org/x/sys/unix"
)

func TestTaskOwnerRejectsNonReadPipe(t *testing.T) {
	reader, writer, err := os.Pipe()
	if err != nil {
		t.Fatal(err)
	}
	defer reader.Close()
	defer writer.Close()
	regular, err := os.CreateTemp(t.TempDir(), "lease")
	if err != nil {
		t.Fatal(err)
	}
	defer regular.Close()
	for _, fd := range []int{-1, 1, int(writer.Fd()), int(regular.Fd())} {
		if stop, err := watchTaskOwner(fd, make(chan os.Signal, 1)); err == nil {
			stop()
			t.Fatalf("accepted invalid owner descriptor %d", fd)
		}
	}
}

func TestTaskOwnerEOFRequestsBoundedCancellation(t *testing.T) {
	reader, writer, err := os.Pipe()
	if err != nil {
		t.Fatal(err)
	}
	defer writer.Close()
	signals := make(chan os.Signal, 1)
	fd, err := syscall.Dup(int(reader.Fd()))
	reader.Close()
	if err != nil {
		t.Fatal(err)
	}
	stop, err := watchTaskOwner(fd, signals)
	if err != nil {
		syscall.Close(fd)
		t.Fatal(err)
	}
	defer stop()
	flags, err := unix.FcntlInt(uintptr(fd), unix.F_GETFD, 0)
	if err != nil || flags&unix.FD_CLOEXEC == 0 {
		t.Fatalf("owner pipe may reach workloads: flags=%d error=%v", flags, err)
	}
	select {
	case sig := <-signals:
		t.Fatalf("live caller caused cancellation: %v", sig)
	case <-time.After(20 * time.Millisecond):
	}
	writer.Close()
	select {
	case sig := <-signals:
		if sig != syscall.SIGTERM {
			t.Fatalf("unexpected cancellation signal %v", sig)
		}
	case <-time.After(time.Second):
		t.Fatal("lost caller did not cancel its detached command")
	}
}

func TestTaskOwnerCleanupDoesNotWaitForLiveCaller(t *testing.T) {
	reader, writer, err := os.Pipe()
	if err != nil {
		t.Fatal(err)
	}
	defer writer.Close()
	fd, err := syscall.Dup(int(reader.Fd()))
	reader.Close()
	if err != nil {
		t.Fatal(err)
	}
	stop, err := watchTaskOwner(fd, make(chan os.Signal, 1))
	if err != nil {
		syscall.Close(fd)
		t.Fatal(err)
	}
	done := make(chan struct{})
	go func() { stop(); close(done) }()
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("command cleanup waited for its still-live caller")
	}
}
