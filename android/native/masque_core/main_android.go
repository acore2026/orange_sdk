//go:build android

package main

/*
#include <stdlib.h>
*/
import "C"

import (
	"fmt"
	"sync"
	"sync/atomic"
	"time"

	"github.com/acore2026/orange-sdk/android/masque-core/core"
)

var (
	handleSequence atomic.Int64
	handlesMu      sync.Mutex
	handles        = map[int64]*core.Tunnel{}
)

const (
	statDownlinkPackets = iota + 1
	statDownlinkPacketsOverMTU
	statDownlinkReadBufferTooSmall
	statUplinkDatagramTooLarge
	statMaxDownlinkPacketBytes
)

//export MasqueStart
func MasqueStart(tunFD C.int, udpFD C.int, serverURL, authorization, agentTunCIDR, identityDirectory *C.char, mtu C.int) C.longlong {
	configuration := core.Configuration{
		ServerURL: C.GoString(serverURL), AgentTunCIDR: C.GoString(agentTunCIDR),
		IdentityDirectory: C.GoString(identityDirectory), MTU: int(mtu), ConnectTimeout: 10 * time.Second,
	}
	if authorization != nil {
		configuration.Authorization = C.GoString(authorization)
	}
	tunnel, err := core.Start(int(tunFD), int(udpFD), configuration)
	if err != nil {
		fmt.Printf("agent-masque: start failed: %v\n", err)
		return 0
	}
	handle := handleSequence.Add(1)
	handlesMu.Lock()
	handles[handle] = tunnel
	handlesMu.Unlock()
	return C.longlong(handle)
}

//export MasqueReplaceTun
func MasqueReplaceTun(handle C.longlong, tunFD C.int) C.int {
	handlesMu.Lock()
	tunnel := handles[int64(handle)]
	handlesMu.Unlock()
	if tunnel == nil {
		return 0
	}
	if err := tunnel.ReplaceTun(int(tunFD)); err != nil {
		fmt.Printf("agent-masque: replace TUN failed: %v\n", err)
		return 0
	}
	return 1
}

//export MasqueGetStat
func MasqueGetStat(handle C.longlong, statistic C.int) C.ulonglong {
	handlesMu.Lock()
	tunnel := handles[int64(handle)]
	handlesMu.Unlock()
	if tunnel == nil {
		return 0
	}
	stats := tunnel.Statistics()
	var value uint64
	switch int(statistic) {
	case statDownlinkPackets:
		value = stats.DownlinkPackets
	case statDownlinkPacketsOverMTU:
		value = stats.DownlinkPacketsOverMTU
	case statDownlinkReadBufferTooSmall:
		value = stats.DownlinkReadBufferTooSmall
	case statUplinkDatagramTooLarge:
		value = stats.UplinkDatagramTooLarge
	case statMaxDownlinkPacketBytes:
		value = stats.MaxDownlinkPacketBytes
	}
	return C.ulonglong(value)
}

//export MasqueStop
func MasqueStop(handle C.longlong) {
	handlesMu.Lock()
	tunnel := handles[int64(handle)]
	delete(handles, int64(handle))
	handlesMu.Unlock()
	if tunnel != nil {
		_ = tunnel.Close()
	}
}

func main() {}
