// Package textcore provides small, deterministic text helpers.
package textcore

import "strings"

// Summarize joins trimmed, nonempty labels with a stable separator.
func Summarize(labels []string) string {
	kept := make([]string, 0, len(labels))
	for _, label := range labels {
		if label = strings.TrimSpace(label); label != "" {
			kept = append(kept, label)
		}
	}
	return strings.Join(kept, ", ")
}
