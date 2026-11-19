//go:build android

package main

/*
#include <stdlib.h>
*/
import "C"

import (
	"encoding/json"
	"errors"
	"fmt"
	"sync"
	"time"

	"github.com/acore2026/orange-sdk/android/masque-core/l5"
)

var l5HandlesMu sync.Mutex
var l5Handles = map[int64]*l5.Client{}

func l5Result(value any, err error) *C.char {
	if err != nil {
		value = map[string]any{"error": err.Error()}
		var bindingError *l5.BindingError
		if errors.As(err, &bindingError) {
			value = map[string]any{"error": err.Error(), "code": bindingError.Code}
		}
	}
	data, _ := json.Marshal(value)
	return C.CString(string(data))
}

//export L5Open
func L5Open(configuration *C.char) *C.char {
	var config l5.Config
	if err := json.Unmarshal([]byte(C.GoString(configuration)), &config); err != nil {
		return l5Result(nil, err)
	}
	client, err := l5.Dial(config)
	if err != nil {
		return l5Result(nil, err)
	}
	handle := handleSequence.Add(1)
	l5HandlesMu.Lock()
	l5Handles[handle] = client
	l5HandlesMu.Unlock()
	return l5Result(map[string]any{"handle": handle}, nil)
}

//export L5Call
func L5Call(handle C.longlong, command *C.char) *C.char {
	l5HandlesMu.Lock()
	client := l5Handles[int64(handle)]
	l5HandlesMu.Unlock()
	if client == nil {
		return l5Result(nil, fmt.Errorf("L5 role is closed"))
	}
	var request struct {
		Operation string          `json:"operation"`
		Namespace []string        `json:"namespace"`
		Name      string          `json:"name"`
		ID        uint64          `json:"request_id"`
		Payload   json.RawMessage `json:"payload"`
		Datagram  bool            `json:"datagram"`
	}
	if err := json.Unmarshal([]byte(C.GoString(command)), &request); err != nil {
		return l5Result(nil, err)
	}
	switch request.Operation {
	case "request":
		id, err := client.Request(request.Namespace, request.Name)
		return l5Result(map[string]any{"request_id": id}, err)
	case "send":
		return l5Result(map[string]bool{"submitted": true}, client.Send(request.ID, request.Payload, request.Datagram))
	case "cancel":
		return l5Result(map[string]bool{"cancelled": true}, client.Cancel(request.ID))
	case "poll":
		return l5Result(client.Poll(250*time.Millisecond), nil)
	default:
		return l5Result(nil, fmt.Errorf("unknown L5 operation"))
	}
}

//export L5Close
func L5Close(handle C.longlong) {
	l5HandlesMu.Lock()
	client := l5Handles[int64(handle)]
	delete(l5Handles, int64(handle))
	l5HandlesMu.Unlock()
	if client != nil {
		client.Close()
	}
}
