"""Binary stdio TCP relay; pass the OpenSSH destination via %h and %p."""
import argparse
import os
import socket
import sys
import threading


def relay(host, port):
    if sys.platform == 'win32':
        import msvcrt
        msvcrt.setmode(sys.stdin.fileno(), os.O_BINARY)
        msvcrt.setmode(sys.stdout.fileno(), os.O_BINARY)
    try:
        sock = socket.create_connection((host, port), timeout=8)
        sock.settimeout(None)
    except OSError as exc:
        print('Windows dog SSH relay: ' + str(exc), file=sys.stderr)
        return 1

    def upload():
        try:
            while True:
                data = os.read(sys.stdin.fileno(), 65536)
                if not data:
                    sock.shutdown(socket.SHUT_WR)
                    return
                sock.sendall(data)
        except OSError:
            pass

    threading.Thread(target=upload, daemon=True).start()
    try:
        while True:
            data = sock.recv(65536)
            if not data:
                break
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()
    except (OSError, BrokenPipeError) as exc:
        print('Windows dog SSH relay: ' + str(exc), file=sys.stderr)
        return 1
    finally:
        sock.close()
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', required=True)
    parser.add_argument('--port', type=int, required=True)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error('port must be between 1 and 65535')
    return relay(args.host, args.port)


if __name__ == '__main__':
    sys.exit(main())
