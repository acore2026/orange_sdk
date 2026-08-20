package core

import (
	"context"
	"crypto/tls"
	"errors"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"net/netip"
	"net/url"
	"os"
	"strconv"
	"sync"
	"time"

	connectip "github.com/quic-go/connect-ip-go"
	"github.com/quic-go/quic-go"
	"github.com/quic-go/quic-go/http3"
	"github.com/yosida95/uritemplate/v3"
)

const EmbeddedServerName = "masque.agent.internal"

type Configuration struct {
	ServerURL         string
	Authorization     string
	AgentTunCIDR      string
	MTU               int
	IdentityDirectory string
	ConnectTimeout    time.Duration
}

type Tunnel struct {
	ctx       context.Context
	cancel    context.CancelFunc
	conn      *connectip.Conn
	quicConn  *quic.Conn
	transport *http3.Transport
	packet    net.PacketConn
	mtu       int

	mu     sync.RWMutex
	tun    *os.File
	closed bool
	done   chan struct{}
	once   sync.Once
}

func Start(tunFD, udpFD int, cfg Configuration) (*Tunnel, error) {
	if tunFD < 0 || udpFD < 0 {
		return nil, errors.New("TUN and protected UDP descriptors are required")
	}
	// Adopt the protected socket before validating the remaining configuration so
	// JNI can use one unambiguous rule: Start always consumes udpFD.
	packetFile := os.NewFile(uintptr(udpFD), "agent-masque-udp")
	if packetFile == nil {
		return nil, errors.New("adopt protected UDP descriptor")
	}
	packet, err := net.FilePacketConn(packetFile)
	_ = packetFile.Close()
	if err != nil {
		return nil, fmt.Errorf("adopt protected UDP socket: %w", err)
	}
	success := false
	defer func() {
		if !success {
			_ = packet.Close()
		}
	}()
	if cfg.MTU < 1280 || cfg.MTU > 65535 {
		return nil, fmt.Errorf("invalid MTU %d", cfg.MTU)
	}
	parsedURL, err := url.Parse(cfg.ServerURL)
	if err != nil || parsedURL.Scheme != "https" || parsedURL.Hostname() == "" {
		return nil, errors.New("MASQUE server URL must be an https URL")
	}
	expected, err := netip.ParsePrefix(cfg.AgentTunCIDR)
	if err != nil {
		return nil, fmt.Errorf("parse Agent TUN CIDR: %w", err)
	}
	identity, err := ensureClientIdentity(cfg.IdentityDirectory)
	if err != nil {
		return nil, err
	}
	log.Printf("WARNING: MASQUE server certificate verification is disabled (internal-test-only)")
	tlsConfig := &tls.Config{
		MinVersion:         tls.VersionTLS13,
		ServerName:         EmbeddedServerName,
		InsecureSkipVerify: true, // Internal test profile: accept any server certificate.
		Certificates:       []tls.Certificate{identity.Certificate},
		NextProtos:         []string{http3.NextProtoH3},
	}

	port := parsedURL.Port()
	if port == "" {
		port = "443"
	}
	remote, err := net.ResolveUDPAddr("udp", net.JoinHostPort(parsedURL.Hostname(), port))
	if err != nil {
		return nil, fmt.Errorf("resolve MASQUE server: %w", err)
	}
	timeout := cfg.ConnectTimeout
	if timeout <= 0 {
		timeout = 10 * time.Second
	}
	connectContext, connectCancel := context.WithTimeout(context.Background(), timeout)
	defer connectCancel()
	quicConnection, err := quic.Dial(
		connectContext,
		packet,
		remote,
		tlsConfig,
		&quic.Config{EnableDatagrams: true, MaxIncomingStreams: -1},
	)
	if err != nil {
		return nil, fmt.Errorf("dial MASQUE QUIC: %w", err)
	}
	transport := &http3.Transport{EnableDatagrams: true}
	template, err := uritemplate.New(parsedURL.String())
	if err != nil {
		_ = quicConnection.CloseWithError(0, "invalid URI template")
		_ = transport.Close()
		return nil, fmt.Errorf("parse CONNECT-IP URI: %w", err)
	}
	headers := make(http.Header)
	if cfg.Authorization != "" {
		headers.Set("Authorization", cfg.Authorization)
	}
	connection, response, err := connectip.DialWithHeaders(
		connectContext, transport.NewClientConn(quicConnection), template, headers,
	)
	if err != nil {
		_ = quicConnection.CloseWithError(0, "CONNECT-IP failed")
		_ = transport.Close()
		if response != nil {
			return nil, fmt.Errorf("CONNECT-IP returned %s: %w", response.Status, err)
		}
		return nil, fmt.Errorf("negotiate CONNECT-IP: %w", err)
	}
	prefixes, err := connection.LocalPrefixes(connectContext)
	if err != nil {
		_ = connection.Close()
		_ = quicConnection.CloseWithError(0, "ADDRESS_ASSIGN missing")
		_ = transport.Close()
		return nil, fmt.Errorf("wait for ADDRESS_ASSIGN: %w", err)
	}
	if !containsAddress(prefixes, expected.Addr()) {
		_ = connection.Close()
		_ = quicConnection.CloseWithError(0, "ADDRESS_ASSIGN mismatch")
		_ = transport.Close()
		return nil, fmt.Errorf("server assigned %v, expected %s", prefixes, expected.Addr())
	}
	if _, err := connection.Routes(connectContext); err != nil {
		_ = connection.Close()
		_ = quicConnection.CloseWithError(0, "ROUTE_ADVERTISEMENT missing")
		_ = transport.Close()
		return nil, fmt.Errorf("wait for ROUTE_ADVERTISEMENT: %w", err)
	}
	tun := os.NewFile(uintptr(tunFD), "agent-tun")
	if tun == nil {
		_ = connection.Close()
		_ = quicConnection.CloseWithError(0, "invalid TUN descriptor")
		_ = transport.Close()
		return nil, errors.New("adopt TUN descriptor")
	}
	ctx, cancel := context.WithCancel(context.Background())
	tunnel := &Tunnel{
		ctx: ctx, cancel: cancel, conn: connection, quicConn: quicConnection,
		transport: transport, packet: packet, mtu: cfg.MTU, tun: tun, done: make(chan struct{}),
	}
	success = true
	go tunnel.uplink(tun)
	go tunnel.downlink()
	return tunnel, nil
}

func (t *Tunnel) ReplaceTun(tunFD int) error {
	if tunFD < 0 {
		return errors.New("replacement TUN descriptor is invalid")
	}
	replacement := os.NewFile(uintptr(tunFD), "agent-tun-replacement")
	if replacement == nil {
		return errors.New("adopt replacement TUN descriptor")
	}
	t.mu.Lock()
	if t.closed {
		t.mu.Unlock()
		_ = replacement.Close()
		return net.ErrClosed
	}
	previous := t.tun
	t.tun = replacement
	t.mu.Unlock()
	_ = previous.Close()
	go t.uplink(replacement)
	return nil
}

func (t *Tunnel) Close() error {
	t.once.Do(func() {
		t.cancel()
		t.mu.Lock()
		t.closed = true
		if t.tun != nil {
			_ = t.tun.Close()
		}
		t.mu.Unlock()
		_ = t.conn.Close()
		_ = t.quicConn.CloseWithError(0, "SDK closed")
		_ = t.transport.Close()
		_ = t.packet.Close()
		close(t.done)
	})
	return nil
}

func (t *Tunnel) uplink(source *os.File) {
	buffer := make([]byte, t.mtu)
	for {
		count, err := source.Read(buffer)
		if err != nil {
			if !errors.Is(err, io.EOF) && !errors.Is(err, os.ErrClosed) {
				fmt.Printf("agent-masque: TUN read failed: %v\n", err)
			}
			return
		}
		if count == 0 {
			continue
		}
		packet := append([]byte(nil), buffer[:count]...)
		icmp, err := t.conn.WritePacket(packet)
		if err != nil {
			fmt.Printf("agent-masque: CONNECT-IP uplink failed: %v\n", err)
			return
		}
		if len(icmp) > 0 {
			t.writeTun(icmp)
		}
	}
}

func (t *Tunnel) downlink() {
	buffer := make([]byte, t.mtu)
	for {
		count, err := t.conn.ReadPacket(buffer)
		if err != nil {
			if !errors.Is(err, net.ErrClosed) {
				fmt.Printf("agent-masque: CONNECT-IP downlink failed: %v\n", err)
			}
			_ = t.Close()
			return
		}
		t.writeTun(buffer[:count])
	}
}

func (t *Tunnel) writeTun(packet []byte) {
	t.mu.RLock()
	defer t.mu.RUnlock()
	if t.closed || t.tun == nil {
		return
	}
	if _, err := t.tun.Write(packet); err != nil && !errors.Is(err, os.ErrClosed) {
		fmt.Printf("agent-masque: TUN write failed: %v\n", err)
	}
}

func containsAddress(prefixes []netip.Prefix, expected netip.Addr) bool {
	for _, prefix := range prefixes {
		if prefix.Addr() == expected {
			return true
		}
	}
	return false
}

func EndpointAuthority(rawURL string) (string, error) {
	parsed, err := url.Parse(rawURL)
	if err != nil || parsed.Hostname() == "" {
		return "", errors.New("invalid MASQUE URL")
	}
	port := parsed.Port()
	if port == "" {
		port = "443"
	}
	return net.JoinHostPort(parsed.Hostname(), port), nil
}

func ParseMTU(value string) (int, error) {
	return strconv.Atoi(value)
}
