package core

import "testing"

func TestConnectIPReceiveBufferUsesFullIPPacketCapacity(t *testing.T) {
	if connectIPReceiveBufferBytes != 64*1024 {
		t.Fatalf("unexpected CONNECT-IP receive buffer: %d", connectIPReceiveBufferBytes)
	}
}

func TestTunnelStatisticsSnapshot(t *testing.T) {
	tunnel := &Tunnel{}
	tunnel.downlinkPackets.Store(12)
	tunnel.downlinkPacketsOverMTU.Store(3)
	tunnel.downlinkReadBufferTooSmall.Store(1)
	tunnel.uplinkDatagramTooLarge.Store(2)
	updateMaximum(&tunnel.maxDownlinkPacketBytes, 1400)
	updateMaximum(&tunnel.maxDownlinkPacketBytes, 1200)

	stats := tunnel.Statistics()
	if stats.DownlinkPackets != 12 ||
		stats.DownlinkPacketsOverMTU != 3 ||
		stats.DownlinkReadBufferTooSmall != 1 ||
		stats.UplinkDatagramTooLarge != 2 ||
		stats.MaxDownlinkPacketBytes != 1400 {
		t.Fatalf("unexpected statistics: %+v", stats)
	}
}
