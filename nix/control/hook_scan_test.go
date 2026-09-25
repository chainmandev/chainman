package main

import (
	"bytes"
	"io"
	"reflect"
	"strings"
	"testing"
)

func TestHookAdvertisementValidation(t *testing.T) {
	a, b := strings.Repeat("a", 40), strings.Repeat("b", 64)
	objects, err := advertisedHookObjects([]byte(a + "\trefs/heads/main\n" + a + "\trefs/tags/v1\n" + b + "\trefs/heads/other\n"))
	if err != nil || !reflect.DeepEqual(objects, []string{a, b}) {
		t.Fatalf("advertisement: %v, %v", objects, err)
	}
	for _, body := range []string{"junk", a + " HEAD", a + " refs/heads/main unexpected", strings.Repeat("0", 40) + " refs/heads/main"} {
		if _, err := advertisedHookObjects([]byte(body)); err == nil {
			t.Fatalf("accepted malformed advertisement %q", body)
		}
	}
}

func TestHookAdvertisementOutputIsBounded(t *testing.T) {
	var output hookAdvertisement
	// Hide WriterTo, exercising io.Copy's ReaderFrom optimization boundary too.
	reader := struct{ io.Reader }{bytes.NewReader(make([]byte, (4<<20)+1))}
	if _, err := io.Copy(&output, reader); err == nil || output.body.Len() > 4<<20 {
		t.Fatal("unbounded remote advertisement")
	}
}
