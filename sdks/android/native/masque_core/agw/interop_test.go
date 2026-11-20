package agw

import (
	"bufio"
	"encoding/json"
	"io"
	"net"
	"os"
	"os/exec"
	"strconv"
	"strings"
	"testing"
	"time"
)

func TestGoClientInteroperatesWithPythonRelay(t *testing.T) {
	root := os.Getenv("ACN_MOQ_PY_ROOT")
	if root == "" {
		t.Skip("set ACN_MOQ_PY_ROOT to the sibling moq-py source for real QUIC interoperability")
	}
	socket, err := net.ListenPacket("udp4", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	port := socket.LocalAddr().(*net.UDPAddr).Port
	socket.Close()
	script := `
import asyncio,sys
sys.path.insert(0,sys.argv[1])
from moq.acn.relay import AcnRelay
from moq.acn.client import AcnClient
from moq.acn.contracts import full_track
async def main():
    port=int(sys.argv[2]); relay=AcnRelay('127.0.0.1',port)
    await relay.start(); owner=object(); relay.open_management(owner)
    for phase in ('PREPARED','ACTIVE'): relay.apply(owner,'g',1,phase,['A','B'])
    publisher=AcnClient('127.0.0.1',port,'B','publisher')
    await publisher.connect(); track=await publisher.request(full_track('g',1,'B','reply'))
    async def echo(request,value): await publisher.send(track,{'reply':value})
    subscriber=AcnClient('127.0.0.1',port,'B','subscriber',on_object=echo)
    await subscriber.connect()
    print('READY',flush=True)
    while True:
        try: await subscriber.request(full_track('g',1,'A','status')); break
        except Exception: await asyncio.sleep(.01)
    await asyncio.Event().wait()
asyncio.run(main())
`
	process := exec.Command("python3", "-B", "-u", "-c", script, root, strconv.Itoa(port))
	stdout, err := process.StdoutPipe()
	if err != nil {
		t.Fatal(err)
	}
	process.Stderr = io.Discard
	if err = process.Start(); err != nil {
		t.Fatal(err)
	}
	defer func() { process.Process.Kill(); process.Wait() }()
	ready := make(chan string, 1)
	go func() { line, _ := bufio.NewReader(stdout).ReadString('\n'); ready <- line }()
	select {
	case line := <-ready:
		if strings.TrimSpace(line) != "READY" {
			t.Fatal("Python Relay failed to start", line)
		}
	case <-time.After(10 * time.Second):
		t.Fatal("Python Relay startup timed out")
	}
	publisher, err := Dial(Config{Host: "127.0.0.1", Port: port, SourceIP: "127.0.0.1", AgentID: "A", Role: "publisher"})
	if err != nil {
		t.Fatal(err)
	}
	defer publisher.Close()
	subscriber, err := Dial(Config{Host: "127.0.0.1", Port: port, SourceIP: "127.0.0.1", AgentID: "A", Role: "subscriber"})
	if err != nil {
		t.Fatal(err)
	}
	defer subscriber.Close()
	namespaceA := []string{"acn", "v1", "group", "", "version", "1", "agent", ""}
	// Generate exact key strings with the standard implementation rather than
	// duplicating the hash algorithm inside the native transport.
	keys, err := exec.Command("python3", "-B", "-c", "import sys;sys.path.insert(0,sys.argv[1]);from moq.acn.contracts import identifier_key;print(identifier_key('g'));print(identifier_key('A'));print(identifier_key('B'))", root).Output()
	if err != nil {
		t.Fatal(err)
	}
	parts := strings.Fields(string(keys))
	namespaceA[3] = parts[0]
	namespaceA[7] = parts[1]
	pubID, err := publisher.Request(namespaceA, "status")
	if err != nil {
		t.Fatal(err)
	}
	if event := publisher.Poll(3 * time.Second); event.Type != "accepted" || event.RequestID != pubID {
		t.Fatal(event)
	}
	namespaceB := append([]string(nil), namespaceA...)
	namespaceB[7] = parts[2]
	subID, err := subscriber.Request(namespaceB, "reply")
	if err != nil {
		t.Fatal(err)
	}
	if event := subscriber.Poll(3 * time.Second); event.Type != "accepted" || event.RequestID != subID {
		t.Fatal(event)
	}
	time.Sleep(50 * time.Millisecond) // Allow B's explicit subscription to be accepted.
	if err = publisher.Send(pubID, []byte(`{"value":"中文与 emoji 🐕"}`), false); err != nil {
		t.Fatal(err)
	}
	event := subscriber.Poll(3 * time.Second)
	if event.Type != "object" {
		t.Fatal(event)
	}
	var value struct {
		Reply struct {
			Value string `json:"value"`
		} `json:"reply"`
	}
	if err = json.Unmarshal(event.Payload, &value); err != nil || value.Reply.Value != "中文与 emoji 🐕" {
		t.Fatal(string(event.Payload), err)
	}
}
