package agw

import (
	"bytes"
	"context"
	"crypto/tls"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"sync"
	"time"

	"github.com/quic-go/quic-go"
)

type Config struct {
	Host     string `json:"host"`
	Port     int    `json:"port"`
	SourceIP string `json:"source_ip"`
	AgentID  string `json:"agent_id"`
	Role     string `json:"role"`
}

type BindingError struct{ Code string }

func (e *BindingError) Error() string { return "ACN binding rejected: " + e.Code }

type Event struct {
	Type          string          `json:"type"`
	RequestID     uint64          `json:"request_id"`
	Code          uint64          `json:"code,omitempty"`
	RetryInterval uint64          `json:"retry_interval,omitempty"`
	Reason        string          `json:"reason,omitempty"`
	Payload       json.RawMessage `json:"payload,omitempty"`
}
type track struct {
	ID       uint64
	Stream   *quic.SendStream
	Receive  *quic.ReceiveStream
	Object   uint64
	Accepted bool
	Mu       sync.Mutex
}
type Client struct {
	config  Config
	conn    *quic.Conn
	packet  *net.UDPConn
	control *quic.Stream
	ctx     context.Context
	cancel  context.CancelFunc
	mu      sync.Mutex
	sendMu  sync.Mutex
	tracks  map[uint64]*track
	next    uint64
	events  chan Event
	closed  bool
}

func Dial(config Config) (*Client, error) {
	if net.ParseIP(config.Host).To4() == nil || config.Port < 1 || config.Port > 65535 || config.AgentID == "" || (config.Role != "publisher" && config.Role != "subscriber") {
		return nil, fmt.Errorf("invalid ACN MOQ configuration")
	}
	if net.ParseIP(config.SourceIP).To4() == nil {
		return nil, fmt.Errorf("PDU UE IPv4 required")
	}
	packet, err := net.ListenUDP("udp4", &net.UDPAddr{IP: net.ParseIP(config.SourceIP)})
	if err != nil {
		return nil, err
	}
	ctx, cancel := context.WithCancel(context.Background())
	deadline, done := context.WithTimeout(ctx, 5*time.Second)
	defer done()
	conn, err := quic.Dial(deadline, packet, &net.UDPAddr{IP: net.ParseIP(config.Host), Port: config.Port},
		&tls.Config{MinVersion: tls.VersionTLS13, InsecureSkipVerify: true, NextProtos: []string{"moq-00"}},
		&quic.Config{EnableDatagrams: true, KeepAlivePeriod: 15 * time.Second})
	if err != nil {
		cancel()
		packet.Close()
		return nil, err
	}
	client := &Client{config: config, conn: conn, packet: packet, ctx: ctx, cancel: cancel, tracks: map[uint64]*track{}, events: make(chan Event, 256)}
	client.control, err = conn.OpenStreamSync(deadline)
	if err != nil {
		client.Close()
		return nil, err
	}
	role := uint64(1)
	if config.Role == "subscriber" {
		role = 2
	}
	data, _ := json.Marshal(map[string]any{"binding_version": 1, "agent_id": config.AgentID, "role": config.Role})
	payload := append(integer(Version), integer(role)...)
	payload = append(payload, integer(1)...)
	payload = append(payload, integer(Binding)...)
	payload = append(payload, field(data)...)
	if err = client.writeControl(frame(1, payload)); err != nil {
		client.Close()
		return nil, err
	}
	client.control.SetReadDeadline(time.Now().Add(5 * time.Second))
	kind, err := readInteger(client.control)
	if err != nil {
		client.Close()
		return nil, err
	}
	data, err = readField(client.control, 131072)
	if err != nil || kind != 1 {
		client.Close()
		return nil, fmt.Errorf("Relay SETUP missing: %v", err)
	}
	reader := bytes.NewReader(data)
	version, err := readInteger(reader)
	if err != nil || version != Version {
		client.Close()
		return nil, fmt.Errorf("Relay protocol version mismatch")
	}
	if _, err = readInteger(reader); err != nil {
		client.Close()
		return nil, err
	}
	count, err := readInteger(reader)
	if err != nil {
		client.Close()
		return nil, err
	}
	previous, found := uint64(0), false
	for i := uint64(0); i < count; i++ {
		delta, err := readInteger(reader)
		if err != nil {
			client.Close()
			return nil, err
		}
		previous += delta
		if previous%2 == 0 {
			if _, err = readInteger(reader); err != nil {
				client.Close()
				return nil, err
			}
			continue
		}
		value, err := readField(reader, 65535)
		if err != nil {
			client.Close()
			return nil, err
		}
		if previous == BindingResult {
			if found || len(value) > 4096 {
				client.Close()
				return nil, fmt.Errorf("invalid ACN binding result")
			}
			found = true
			var result struct {
				Version int    `json:"binding_version"`
				AgentID string `json:"agent_id"`
				Role    string `json:"role"`
				Status  string `json:"status"`
				Code    string `json:"code"`
			}
			if err = strictJSON(value, &result); err != nil {
				client.Close()
				return nil, err
			}
			if result.Status != "ACCEPTED" {
				client.Close()
				return nil, &BindingError{Code: result.Code}
			}
			if result.Version != 1 || result.AgentID != config.AgentID || result.Role != config.Role {
				client.Close()
				return nil, fmt.Errorf("ACN binding result mismatch")
			}
		}
	}
	if !found || reader.Len() != 0 {
		client.Close()
		return nil, fmt.Errorf("ACN binding result missing or malformed")
	}
	client.control.SetReadDeadline(time.Time{})
	go client.readControl()
	go client.readStreams()
	go func() {
		<-conn.Context().Done()
		code := uint64(0)
		if cause, ok := context.Cause(conn.Context()).(*quic.ApplicationError); ok {
			code = uint64(cause.ErrorCode)
		}
		client.emit(Event{Type: "closed", Code: code})
		client.cancel()
	}()
	return client, nil
}

func (c *Client) writeControl(data []byte) error {
	c.sendMu.Lock()
	defer c.sendMu.Unlock()
	c.control.SetWriteDeadline(time.Now().Add(5 * time.Second))
	_, err := c.control.Write(data)
	return err
}

func (c *Client) emit(event Event) {
	select {
	case c.events <- event:
	default:
		c.conn.CloseWithError(2, "ACN event queue exceeded")
		c.cancel()
	}
}

func (c *Client) readControl() {
	defer c.conn.CloseWithError(2, "ACN control Stream ended")
	for {
		kind, err := readInteger(c.control)
		if err != nil {
			return
		}
		payload, err := readField(c.control, 131072)
		if err != nil {
			c.conn.CloseWithError(2, "invalid control frame")
			return
		}
		reader := bytes.NewReader(payload)
		id, err := readInteger(reader)
		if err != nil {
			c.conn.CloseWithError(2, "invalid request ID")
			return
		}
		switch kind {
		case 5, 8:
			if (kind == 8) != (c.config.Role == "publisher") {
				c.conn.CloseWithError(2, "wrong response role")
				return
			}
			c.mu.Lock()
			if value := c.tracks[id]; value != nil {
				value.Accepted = true
			}
			c.mu.Unlock()
			c.emit(Event{Type: "accepted", RequestID: id})
		case 3, 9:
			code, err := readInteger(reader)
			if err != nil {
				return
			}
			reason, err := readField(reader, 1024)
			if err != nil {
				return
			}
			retry := uint64(0)
			if kind == 3 && reader.Len() > 0 {
				retry, err = readInteger(reader)
				if err != nil {
					return
				}
			}
			c.mu.Lock()
			request := c.tracks[id]
			delete(c.tracks, id)
			var receive *quic.ReceiveStream
			if request != nil {
				receive = request.Receive
			}
			c.mu.Unlock()
			if receive != nil {
				receive.CancelRead(quic.StreamErrorCode(code))
			}
			c.emit(Event{Type: "failed", RequestID: id, Code: code, RetryInterval: retry, Reason: string(reason)})
		default:
			c.conn.CloseWithError(2, "unexpected ACN control message")
			return
		}
	}
}

func (c *Client) Request(namespace []string, name string) (uint64, error) {
	if len(namespace) != 8 || len([]byte(name)) == 0 || len([]byte(name)) > 128 {
		return 0, fmt.Errorf("invalid ACN Track")
	}
	c.mu.Lock()
	id := c.next
	c.next++
	c.tracks[id] = &track{ID: id}
	c.mu.Unlock()
	payload := append(integer(id), integer(id)...)
	payload = append(payload, integer(uint64(len(namespace)))...)
	for _, part := range namespace {
		payload = append(payload, field([]byte(part))...)
	}
	payload = append(payload, field([]byte(name))...)
	kind := uint64(7)
	if c.config.Role == "subscriber" {
		kind = 4
		payload = append(payload, 128, 0, 2)
	}
	err := c.writeControl(frame(kind, payload))
	if err != nil {
		c.mu.Lock()
		delete(c.tracks, id)
		c.mu.Unlock()
	}
	return id, err
}

func (c *Client) Send(id uint64, payload []byte, datagram bool) error {
	if c.config.Role != "publisher" || len(payload) > 65536 {
		return fmt.Errorf("invalid publication payload")
	}
	var value map[string]json.RawMessage
	if err := strictJSON(payload, &value); err != nil || value == nil {
		return fmt.Errorf("expected JSON Object")
	}
	for len(payload) < 5 {
		payload = append(payload, ' ')
	}
	c.mu.Lock()
	request := c.tracks[id]
	accepted := request != nil && request.Accepted
	c.mu.Unlock()
	if !accepted {
		return fmt.Errorf("publication is not accepted")
	}
	request.Mu.Lock()
	defer request.Mu.Unlock()
	oid := request.Object
	request.Object++
	if datagram {
		data := append(integer(id), integer(0)...)
		data = append(data, integer(oid)...)
		data = append(data, 128)
		data = append(data, field(payload)...)
		return c.conn.SendDatagram(data)
	}
	if request.Stream == nil {
		ctx, cancel := context.WithTimeout(c.ctx, 5*time.Second)
		defer cancel()
		stream, err := c.conn.OpenUniStreamSync(ctx)
		if err != nil {
			return err
		}
		request.Stream = stream
		go func() {
			<-stream.Context().Done()
			c.mu.Lock()
			valid := c.tracks[id] == request
			if valid {
				delete(c.tracks, id)
			}
			c.mu.Unlock()
			if valid {
				c.emit(Event{Type: "failed", RequestID: id, Reason: fmt.Sprintf("publication Stream stopped: %v", context.Cause(stream.Context()))})
			}
		}()
		header := append(integer(1), integer(id)...)
		header = append(header, 0, 0, 128)
		stream.SetWriteDeadline(time.Now().Add(5 * time.Second))
		if _, err = stream.Write(header); err != nil {
			return err
		}
	}
	request.Stream.SetWriteDeadline(time.Now().Add(5 * time.Second))
	object := append(integer(oid), field(payload)...)
	_, err := request.Stream.Write(object)
	return err
}

func (c *Client) Cancel(id uint64) error {
	c.mu.Lock()
	value := c.tracks[id]
	delete(c.tracks, id)
	var receive *quic.ReceiveStream
	if value != nil {
		receive = value.Receive
	}
	c.mu.Unlock()
	if value == nil {
		return nil
	}
	if c.config.Role == "publisher" {
		payload := append(integer(id), integer(0)...)
		payload = append(payload, field([]byte("publisher ended"))...)
		value.Mu.Lock()
		if value.Stream != nil {
			value.Stream.CancelWrite(0)
		}
		value.Mu.Unlock()
		return c.writeControl(frame(9, payload))
	}
	if receive != nil {
		receive.CancelRead(0)
	}
	return nil
}

func (c *Client) readStreams() {
	for {
		stream, err := c.conn.AcceptUniStream(c.ctx)
		if err != nil {
			return
		}
		if c.config.Role != "subscriber" {
			stream.CancelRead(2)
			continue
		}
		go c.readObjects(stream)
	}
}

func (c *Client) readObjects(stream *quic.ReceiveStream) {
	stream.SetReadDeadline(time.Now().Add(5 * time.Second))
	kind, err := readInteger(stream)
	if err != nil || kind != 1 {
		stream.CancelRead(2)
		return
	}
	alias, err := readInteger(stream)
	if err != nil {
		return
	}
	if _, err = readInteger(stream); err != nil {
		return
	}
	if _, err = readInteger(stream); err != nil {
		return
	}
	var priority [1]byte
	if _, err = io.ReadFull(stream, priority[:]); err != nil {
		return
	}
	c.mu.Lock()
	request := c.tracks[alias]
	if request != nil {
		request.Receive = stream
	}
	c.mu.Unlock()
	if request == nil {
		stream.CancelRead(0)
		return
	}
	for {
		stream.SetReadDeadline(time.Time{})
		var first [1]byte
		if _, err = io.ReadFull(stream, first[:]); err != nil {
			break
		}
		stream.SetReadDeadline(time.Now().Add(5 * time.Second))
		if _, err = readInteger(io.MultiReader(bytes.NewReader(first[:]), stream)); err != nil {
			break
		}
		size, readErr := readInteger(stream)
		if readErr != nil || size > 65536 || size < 5 {
			err = fmt.Errorf("invalid ACN Object length")
			break
		}
		payload := make([]byte, int(size))
		if _, err = io.ReadFull(stream, payload); err != nil {
			break
		}
		var value map[string]json.RawMessage
		if err = strictJSON(payload, &value); err != nil || value == nil {
			err = fmt.Errorf("invalid JSON Object")
			break
		}
		c.mu.Lock()
		valid := c.tracks[alias] == request
		c.mu.Unlock()
		if !valid {
			return
		}
		c.emit(Event{Type: "object", RequestID: alias, Payload: payload})
	}
	stream.CancelRead(2)
	c.mu.Lock()
	valid := c.tracks[alias] == request
	if valid {
		delete(c.tracks, alias)
	}
	c.mu.Unlock()
	if valid {
		c.emit(Event{Type: "failed", RequestID: alias, Reason: fmt.Sprintf("Object Stream ended: %v", err)})
	}
}

func (c *Client) Poll(timeout time.Duration) Event {
	select {
	case event := <-c.events:
		return event
	case <-time.After(timeout):
		return Event{Type: "idle"}
	case <-c.ctx.Done():
		select {
		case event := <-c.events:
			return event
		default:
			return Event{Type: "closed"}
		}
	}
}
func (c *Client) Close() {
	c.mu.Lock()
	if c.closed {
		c.mu.Unlock()
		return
	}
	c.closed = true
	c.mu.Unlock()
	c.cancel()
	c.conn.CloseWithError(0, "SDK role closed")
	c.packet.Close()
}
