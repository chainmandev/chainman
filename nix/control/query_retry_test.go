package main

import (
	"context"
	"errors"
	"fmt"
	"os/exec"
	"testing"
)

func TestReadOnlyQueryResamplesOnlySuccessfulPipeDrainTimeout(t *testing.T) {
	type response struct {
		output string
		err    error
	}
	failure := errors.New("backend failed")
	drain := fmt.Errorf("probe: %w", exec.ErrWaitDelay)
	for _, test := range []struct {
		name      string
		responses []response
		want      string
		wantError error
	}{
		{"success", []response{{"complete", nil}}, "complete", nil},
		{"drain timeout", []response{{"partial", drain}, {"fresh complete", nil}}, "fresh complete", nil},
		{"persistent drain timeout", []response{{"partial", drain}, {"partial again", drain}}, "partial again", exec.ErrWaitDelay},
		{"backend failure", []response{{"failed", failure}}, "failed", failure},
		{"deadline failure", []response{{"", context.DeadlineExceeded}}, "", context.DeadlineExceeded},
	} {
		t.Run(test.name, func(t *testing.T) {
			calls := 0
			output, err := readOnlyQuery(context.Background(), func() ([]byte, error) {
				if calls >= len(test.responses) {
					t.Fatal("unexpected query replay")
				}
				response := test.responses[calls]
				calls++
				return []byte(response.output), response.err
			})
			if string(output) != test.want || !errors.Is(err, test.wantError) || calls != len(test.responses) {
				t.Fatalf("output=%q error=%v calls=%d", output, err, calls)
			}
		})
	}
}

func TestReadOnlyQueryDoesNotReplayAfterCancellation(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	calls := 0
	_, err := readOnlyQuery(ctx, func() ([]byte, error) {
		calls++
		cancel()
		return []byte("partial"), exec.ErrWaitDelay
	})
	if calls != 1 || !errors.Is(err, exec.ErrWaitDelay) {
		t.Fatalf("error=%v calls=%d", err, calls)
	}
}

func TestReadOnlyQueryDoesNotStartAfterCancellation(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	_, err := readOnlyQuery(ctx, func() ([]byte, error) {
		t.Fatal("query started after cancellation")
		return nil, nil
	})
	if !errors.Is(err, context.Canceled) {
		t.Fatal(err)
	}
}
