package main

// A callback's finite supervisor may outlive the lefthook job that launched it.
// Its inherited shared lock makes that cleanup visible to the outer hook, even
// when lefthook kills the callback client and closes its output immediately.
import (
	"fmt"
	"os"
	"syscall"
	"time"
)

func hookCallbackLease() (*os.File, error) {
	path := os.Getenv("CHAINMAN_HOOK_CALLBACK_BARRIER")
	if path == "" {
		return nil, nil
	}
	f, err := os.OpenFile(path, os.O_RDONLY|syscall.O_NOFOLLOW|syscall.O_NONBLOCK, 0)
	if err != nil {
		return nil, err
	}
	if err = syscall.Flock(int(f.Fd()), syscall.LOCK_SH|syscall.LOCK_NB); err == nil {
		var info os.FileInfo
		info, err = f.Stat()
		if err == nil && (!info.Mode().IsRegular() || info.Size() != 0) {
			err = fmt.Errorf("hook callback scope is closed")
		}
	}
	if err != nil {
		f.Close()
		return nil, err
	}
	return f, nil
}

func hookDrainCallbacks(f *os.File) error {
	// Callback supervisors have a seven-second cleanup deadline. This wait is
	// independent of inherited stdout/stderr, which lefthook may stop reading.
	deadline := time.Now().Add(8 * time.Second)
	for {
		err := syscall.Flock(int(f.Fd()), syscall.LOCK_EX|syscall.LOCK_NB)
		if err == nil {
			// Keep the closed marker after releasing the lock: a delayed callback
			// must refuse to start. The operation's private directory owns it.
			_, err = f.WriteAt([]byte("closed\n"), 0)
			return err
		}
		if err != syscall.EWOULDBLOCK && err != syscall.EAGAIN {
			return err
		}
		if time.Now().After(deadline) {
			return fmt.Errorf("hook callback cleanup did not finish; inspect remaining hook processes before retrying")
		}
		time.Sleep(25 * time.Millisecond)
	}
}
