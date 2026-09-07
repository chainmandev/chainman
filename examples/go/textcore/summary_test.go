package textcore

import "testing"

func TestSummarizeOmitsBlankLabels(t *testing.T) {
	t.Parallel()
	if got := Summarize([]string{" first ", "", " second"}); got != "first, second" {
		t.Fatalf("Summarize() = %q", got)
	}
}
