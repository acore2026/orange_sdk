package connectip

import (
	"errors"
	"io"
	"testing"
)

func TestCopyPacketRejectsShortBufferWithoutPartialData(t *testing.T) {
	destination := make([]byte, 4)
	count, err := copyPacket(destination, []byte{1, 2, 3, 4, 5})
	if count != 0 {
		t.Fatalf("expected zero copied bytes, got %d", count)
	}
	if !errors.Is(err, io.ErrShortBuffer) {
		t.Fatalf("expected io.ErrShortBuffer, got %v", err)
	}
}

func TestCopyPacketCopiesCompletePacket(t *testing.T) {
	destination := make([]byte, 8)
	count, err := copyPacket(destination, []byte{1, 2, 3, 4})
	if err != nil {
		t.Fatalf("copyPacket returned %v", err)
	}
	if count != 4 {
		t.Fatalf("expected four copied bytes, got %d", count)
	}
}
