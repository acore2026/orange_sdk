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

	"github.com/acore2026/orange-sdk/android/masque-core/agw"
)

var agwHandlesMu sync.Mutex
var agwHandles = map[int64]*agw.Client{}

func agwResult(value any, err error) *C.char {
	if err != nil {
		value = map[string]any{"error": err.Error()}
		var bindingError *agw.BindingError
		if errors.As(err, &bindingError) {
			value = map[string]any{"error": err.Error(), "code": bindingError.Code}
		}
	}
	data, _ := json.Marshal(value)
	return C.CString(string(data))
}

//export AGWOpen
func AGWOpen(configuration *C.char) *C.char {
	var config agw.Config
	if err := json.Unmarshal([]byte(C.GoString(configuration)), &config); err != nil {
		return agwResult(nil, err)
	}
	client, err := agw.Dial(config)
	if err != nil {
		return agwResult(nil, err)
	}
	handle := handleSequence.Add(1)
	agwHandlesMu.Lock()
	agwHandles[handle] = client
	agwHandlesMu.Unlock()
	return agwResult(map[string]any{"handle": handle}, nil)
}

//export AGWCall
func AGWCall(handle C.longlong, command *C.char) *C.char {
	agwHandlesMu.Lock()
	client := agwHandles[int64(handle)]
	agwHandlesMu.Unlock()
	if client == nil {
		return agwResult(nil, fmt.Errorf("AGW role is closed"))
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
		return agwResult(nil, err)
	}
	switch request.Operation {
	case "request":
		id, err := client.Request(request.Namespace, request.Name)
		return agwResult(map[string]any{"request_id": id}, err)
	case "send":
		return agwResult(map[string]bool{"submitted": true}, client.Send(request.ID, request.Payload, request.Datagram))
	case "cancel":
		return agwResult(map[string]bool{"cancelled": true}, client.Cancel(request.ID))
	case "poll":
		return agwResult(client.Poll(250*time.Millisecond), nil)
	default:
		return agwResult(nil, fmt.Errorf("unknown AGW operation"))
	}
}

//export AGWClose
func AGWClose(handle C.longlong) {
	agwHandlesMu.Lock()
	client := agwHandles[int64(handle)]
	delete(agwHandles, int64(handle))
	agwHandlesMu.Unlock()
	if client != nil {
		client.Close()
	}
}
