package core

import (
	"crypto/ed25519"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/binary"
	"fmt"
	"math/big"
	"net"
	"net/http"
	"net/netip"
	"net/url"
	"os"
	"testing"
	"time"

	connectip "github.com/quic-go/connect-ip-go"
	"github.com/quic-go/quic-go/http3"
	"github.com/yosida95/uritemplate/v3"
	"golang.org/x/sys/unix"
)

func TestConnectIPTunnelRoundTrip(t *testing.T) {
	serverUDP, err := net.ListenUDP("udp4", &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1)})
	if err != nil {
		t.Fatal(err)
	}
	defer serverUDP.Close()
	port := serverUDP.LocalAddr().(*net.UDPAddr).Port
	endpoint := fmt.Sprintf("https://127.0.0.1:%d/.well-known/masque/ip", port)
	template := uritemplate.MustNew(endpoint)
	serverConnection := make(chan *connectip.Conn, 1)
	clientCertificateSeen := make(chan bool, 1)
	authorizationSeen := make(chan string, 1)
	proxy := &connectip.Proxy{}
	mux := http.NewServeMux()
	mux.HandleFunc("/.well-known/masque/ip", func(writer http.ResponseWriter, request *http.Request) {
		clientCertificateSeen <- request.TLS != nil && len(request.TLS.PeerCertificates) == 1
		authorizationSeen <- request.Header.Get("Authorization")
		parsed, parseErr := connectip.ParseRequest(request, template)
		if parseErr != nil {
			http.Error(writer, parseErr.Error(), http.StatusBadRequest)
			return
		}
		connection, proxyErr := proxy.Proxy(writer, parsed)
		if proxyErr != nil {
			return
		}
		if assignErr := connection.AssignAddresses(request.Context(), []netip.Prefix{
			netip.MustParsePrefix("8.8.8.7/32"),
		}); assignErr != nil {
			return
		}
		if routeErr := connection.AdvertiseRoute(request.Context(), []connectip.IPRoute{{
			StartIP: netip.MustParseAddr("0.0.0.0"),
			EndIP:   netip.MustParseAddr("255.255.255.255"),
		}}); routeErr != nil {
			return
		}
		serverConnection <- connection
		<-request.Context().Done()
	})
	certificate := ephemeralServerIdentity(t, "test.masque.internal")
	server := &http3.Server{
		Handler: mux, EnableDatagrams: true,
		TLSConfig: &tls.Config{
			MinVersion: tls.VersionTLS13, Certificates: []tls.Certificate{certificate},
			ClientAuth: tls.RequestClientCert,
		},
	}
	serverErrors := make(chan error, 1)
	go func() { serverErrors <- server.Serve(serverUDP) }()
	defer server.Close()

	clientUDP, err := net.ListenUDP("udp4", &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1)})
	if err != nil {
		t.Fatal(err)
	}
	udpFile, err := clientUDP.File()
	if err != nil {
		t.Fatal(err)
	}
	_ = clientUDP.Close()

	pair, err := unix.Socketpair(unix.AF_UNIX, unix.SOCK_DGRAM, 0)
	if err != nil {
		t.Fatal(err)
	}
	peerTun := os.NewFile(uintptr(pair[1]), "test-peer-tun")
	defer peerTun.Close()
	tunnel, err := Start(pair[0], int(udpFile.Fd()), Configuration{
		ServerURL: endpoint, AgentTunCIDR: "8.8.8.7/24", MTU: 1280,
		Authorization:     "Bearer native-test-token",
		IdentityDirectory: t.TempDir(), ConnectTimeout: 3 * time.Second,
	})
	if err != nil {
		t.Fatal(err)
	}
	defer tunnel.Close()
	connection := receiveWithin(t, serverConnection)
	if !receiveWithin(t, clientCertificateSeen) {
		t.Fatal("SDK client certificate was not presented")
	}
	if authorization := receiveWithin(t, authorizationSeen); authorization != "Bearer native-test-token" {
		t.Fatalf("unexpected Authorization header %q", authorization)
	}

	uplink := ipv4Packet(netip.MustParseAddr("8.8.8.7"), netip.MustParseAddr("8.8.8.8"))
	if _, err := peerTun.Write(uplink); err != nil {
		t.Fatal(err)
	}
	uplinkResult := make(chan []byte, 1)
	go func() {
		buffer := make([]byte, 1280)
		count, readErr := connection.ReadPacket(buffer)
		if readErr == nil {
			uplinkResult <- append([]byte(nil), buffer[:count]...)
		}
	}()
	assertPacketEndpoints(t, receiveWithin(t, uplinkResult), "8.8.8.7", "8.8.8.8")

	downlink := ipv4Packet(netip.MustParseAddr("8.8.8.8"), netip.MustParseAddr("8.8.8.7"))
	if _, err := connection.WritePacket(downlink); err != nil {
		t.Fatal(err)
	}
	downlinkResult := make(chan []byte, 1)
	go func() {
		buffer := make([]byte, 1280)
		count, readErr := peerTun.Read(buffer)
		if readErr == nil {
			downlinkResult <- append([]byte(nil), buffer[:count]...)
		}
	}()
	assertPacketEndpoints(t, receiveWithin(t, downlinkResult), "8.8.8.8", "8.8.8.7")

	parsedURL, _ := url.Parse(endpoint)
	if parsedURL.Path != "/.well-known/masque/ip" {
		t.Fatalf("unexpected endpoint path %q", parsedURL.Path)
	}
	select {
	case err := <-serverErrors:
		if err != nil {
			t.Fatalf("HTTP/3 server stopped early: %v", err)
		}
	default:
	}
}

func ephemeralServerIdentity(t *testing.T, serverName string) tls.Certificate {
	t.Helper()
	now := time.Now()
	rootPublic, rootPrivate, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	rootTemplate := &x509.Certificate{
		SerialNumber:          big.NewInt(1),
		Subject:               pkix.Name{CommonName: "Agent MASQUE test root"},
		NotBefore:             now.Add(-time.Minute),
		NotAfter:              now.Add(time.Hour),
		IsCA:                  true,
		BasicConstraintsValid: true,
		KeyUsage:              x509.KeyUsageCertSign | x509.KeyUsageCRLSign,
	}
	rootDER, err := x509.CreateCertificate(rand.Reader, rootTemplate, rootTemplate, rootPublic, rootPrivate)
	if err != nil {
		t.Fatal(err)
	}
	root, err := x509.ParseCertificate(rootDER)
	if err != nil {
		t.Fatal(err)
	}
	serverPublic, serverPrivate, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	serverTemplate := &x509.Certificate{
		SerialNumber: big.NewInt(2),
		Subject:      pkix.Name{CommonName: serverName},
		NotBefore:    now.Add(-time.Minute),
		NotAfter:     now.Add(time.Hour),
		DNSNames:     []string{serverName},
		KeyUsage:     x509.KeyUsageDigitalSignature,
		ExtKeyUsage:  []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth},
	}
	serverDER, err := x509.CreateCertificate(rand.Reader, serverTemplate, root, serverPublic, rootPrivate)
	if err != nil {
		t.Fatal(err)
	}
	return tls.Certificate{
		Certificate: [][]byte{serverDER, rootDER}, PrivateKey: serverPrivate,
	}
}

func receiveWithin[T any](t *testing.T, values <-chan T) T {
	t.Helper()
	select {
	case value := <-values:
		return value
	case <-time.After(3 * time.Second):
		var zero T
		t.Fatal("timed out")
		return zero
	}
}

func ipv4Packet(source, destination netip.Addr) []byte {
	packet := make([]byte, 28)
	packet[0] = 0x45
	binary.BigEndian.PutUint16(packet[2:4], uint16(len(packet)))
	packet[8] = 64
	packet[9] = 17
	copy(packet[12:16], source.AsSlice())
	copy(packet[16:20], destination.AsSlice())
	binary.BigEndian.PutUint16(packet[20:22], 4000)
	binary.BigEndian.PutUint16(packet[22:24], 4001)
	binary.BigEndian.PutUint16(packet[24:26], 8)
	binary.BigEndian.PutUint16(packet[10:12], checksum(packet[:20]))
	return packet
}

func checksum(header []byte) uint16 {
	var sum uint32
	for index := 0; index < len(header); index += 2 {
		sum += uint32(binary.BigEndian.Uint16(header[index : index+2]))
	}
	for sum > 0xffff {
		sum = (sum >> 16) + (sum & 0xffff)
	}
	return ^uint16(sum)
}

func assertPacketEndpoints(t *testing.T, packet []byte, source, destination string) {
	t.Helper()
	if len(packet) < 20 {
		t.Fatalf("packet is too short: %d", len(packet))
	}
	actualSource := netip.AddrFrom4([4]byte(packet[12:16]))
	actualDestination := netip.AddrFrom4([4]byte(packet[16:20]))
	if actualSource.String() != source || actualDestination.String() != destination {
		t.Fatalf("unexpected endpoints %s -> %s", actualSource, actualDestination)
	}
}
