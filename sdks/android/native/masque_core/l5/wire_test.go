package l5

import (
	"bytes"
	"encoding/hex"
	"math"
	"testing"
)

func TestRetainedMOQIntegerEncoding(t *testing.T) {
	for _, value := range []uint64{0, 127, 128, 16129, 16131, 16383, 16384, 1<<21 - 1, 1 << 21, 1 << 28, 1 << 35, 1 << 42, 1 << 56, math.MaxUint64} {
		data := integer(value)
		decoded, err := readInteger(bytes.NewReader(data))
		if err != nil || decoded != value {
			t.Fatalf("%d: %x -> %d %v", value, data, decoded, err)
		}
	}
	if got := hex.EncodeToString(integer(Binding)); got != "bf01" {
		t.Fatal(got)
	}
	if got := hex.EncodeToString(integer(BindingResult)); got != "bf03" {
		t.Fatal(got)
	}
}

func TestStrictJSONRejectsDuplicateNestedFields(t *testing.T) {
	var value map[string]any
	for _, data := range []string{`{"x":1,"x":2}`, `{"x":{"a":1,"a":2}}`, `{"a":1} {}`} {
		if strictJSON([]byte(data), &value) == nil {
			t.Fatal("accepted", data)
		}
	}
	if err := strictJSON([]byte(`{"x":{"a":2}}`), &value); err != nil {
		t.Fatal(err)
	}
}
