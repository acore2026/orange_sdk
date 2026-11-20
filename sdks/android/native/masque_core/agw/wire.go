// Package agw implements the ACN-only MOQT profile without modifying CONNECT-IP.
package agw

import (
	"bytes"
	"encoding/binary"
	"encoding/json"
	"fmt"
	"io"
)

const (
	Version         = uint64(0xFF000011)
	Binding         = uint64(0x3F01)
	BindingResult   = uint64(0x3F03)
	SessionReplaced = uint64(0xAC10)
)

// This is moq-py's retained integer format, not QUIC's varint format.
func integer(value uint64) []byte {
	sizes := []struct {
		bits   uint
		size   int
		prefix byte
	}{{7, 1, 0}, {14, 2, 0x80}, {21, 3, 0xc0}, {28, 4, 0xe0}, {35, 5, 0xf0}, {42, 6, 0xf8}, {56, 8, 0xfe}}
	for _, size := range sizes {
		if value < uint64(1)<<size.bits {
			data := make([]byte, size.size)
			for i := size.size - 1; i >= 0; i-- {
				data[i] = byte(value)
				value >>= 8
			}
			data[0] |= size.prefix
			return data
		}
	}
	data := make([]byte, 9)
	data[0] = 0xff
	binary.BigEndian.PutUint64(data[1:], value)
	return data
}

func readInteger(reader io.Reader) (uint64, error) {
	var first [1]byte
	if _, err := io.ReadFull(reader, first[:]); err != nil {
		return 0, err
	}
	value := uint64(first[0])
	size := 1
	switch {
	case first[0] < 0x80:
		return value, nil
	case first[0] < 0xc0:
		size = 2
		value &= 0x3f
	case first[0] < 0xe0:
		size = 3
		value &= 0x1f
	case first[0] < 0xf0:
		size = 4
		value &= 0x0f
	case first[0] < 0xf8:
		size = 5
		value &= 7
	case first[0] < 0xfc:
		size = 6
		value &= 3
	case first[0] == 0xfe:
		size = 8
		value = 0
	case first[0] == 0xff:
		size = 9
		value = 0
	default:
		return 0, fmt.Errorf("invalid MOQ integer prefix")
	}
	data := make([]byte, size-1)
	if _, err := io.ReadFull(reader, data); err != nil {
		return 0, err
	}
	for _, b := range data {
		value = (value << 8) | uint64(b)
	}
	return value, nil
}

func field(data []byte) []byte                 { return append(integer(uint64(len(data))), data...) }
func frame(kind uint64, payload []byte) []byte { return append(integer(kind), field(payload)...) }

func readField(reader io.Reader, maximum uint64) ([]byte, error) {
	size, err := readInteger(reader)
	if err != nil {
		return nil, err
	}
	if size > maximum {
		return nil, fmt.Errorf("MOQ field size exceeded")
	}
	data := make([]byte, int(size))
	_, err = io.ReadFull(reader, data)
	return data, err
}

func strictJSON(data []byte, value any) error {
	decoder := json.NewDecoder(bytes.NewReader(data))
	var walk func() error
	walk = func() error {
		token, err := decoder.Token()
		if err != nil {
			return err
		}
		if delim, ok := token.(json.Delim); ok {
			switch delim {
			case '{':
				seen := map[string]bool{}
				for decoder.More() {
					key, err := decoder.Token()
					if err != nil {
						return err
					}
					name, ok := key.(string)
					if !ok || seen[name] {
						return fmt.Errorf("duplicate JSON field")
					}
					seen[name] = true
					if err := walk(); err != nil {
						return err
					}
				}
			case '[':
				for decoder.More() {
					if err := walk(); err != nil {
						return err
					}
				}
			default:
				return fmt.Errorf("unexpected JSON delimiter")
			}
			_, err = decoder.Token()
			return err
		}
		return nil
	}
	if err := walk(); err != nil {
		return err
	}
	if _, err := decoder.Token(); err != io.EOF {
		return fmt.Errorf("trailing JSON content")
	}
	return json.Unmarshal(data, value)
}
