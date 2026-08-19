from __future__ import annotations

import asyncio
import fcntl
import os
import struct
from ipaddress import ip_interface

from .errors import AgentSdkError, ErrorCode

TUNSETIFF = 0x400454CA
IFF_TUN = 0x0001
IFF_NO_PI = 0x1000


class LinuxTunDevice:
    def __init__(self, fd: int, name: str, cidr: str, mtu: int) -> None:
        self._fd = fd
        self.name = name
        self.cidr = cidr
        self.mtu = mtu
        self._closed = False

    @classmethod
    async def create(cls, name: str, cidr: str, mtu: int) -> "LinuxTunDevice":
        fd = -1
        try:
            fd = os.open("/dev/net/tun", os.O_RDWR | os.O_NONBLOCK | os.O_CLOEXEC)
            request = struct.pack("16sH", name.encode(), IFF_TUN | IFF_NO_PI)
            response = fcntl.ioctl(fd, TUNSETIFF, request)
            actual_name = response[:16].split(b"\x00", 1)[0].decode()
            interface = ip_interface(cidr)

            def configure() -> None:
                from pyroute2 import IPRoute

                with IPRoute() as ipr:
                    indices = ipr.link_lookup(ifname=actual_name)
                    if not indices:
                        raise RuntimeError(f"TUN {actual_name} was not created")
                    index = indices[0]
                    ipr.link("set", index=index, mtu=mtu, state="up")
                    ipr.addr(
                        "replace",
                        index=index,
                        address=str(interface.ip),
                        prefixlen=interface.network.prefixlen,
                    )

            await asyncio.to_thread(configure)
            return cls(fd, actual_name, str(interface), mtu)
        except Exception as exc:
            if fd >= 0:
                os.close(fd)
            raise AgentSdkError(
                ErrorCode.TUN_CREATE_FAILED, f"failed to create Linux TUN: {exc}"
            ) from exc

    async def read(self) -> bytes:
        if self._closed:
            return b""
        loop = asyncio.get_running_loop()
        while not self._closed:
            try:
                return os.read(self._fd, self.mtu)
            except BlockingIOError:
                ready = loop.create_future()

                def readable() -> None:
                    if not ready.done():
                        ready.set_result(None)

                loop.add_reader(self._fd, readable)
                try:
                    await ready
                finally:
                    loop.remove_reader(self._fd)
        return b""

    async def write(self, packet: bytes) -> None:
        if self._closed:
            return
        view = memoryview(packet)
        while view and not self._closed:
            try:
                written = os.write(self._fd, view)
                view = view[written:]
            except BlockingIOError:
                await asyncio.sleep(0)

    async def close(self) -> None:
        if not self._closed:
            self._closed = True
            os.close(self._fd)


def validate_ip_packet(packet: bytes, mtu: int) -> tuple[str, str]:
    if not packet or len(packet) > mtu:
        raise ValueError("IP packet is empty or exceeds TUN MTU")
    version = packet[0] >> 4
    if version == 4:
        if len(packet) < 20:
            raise ValueError("truncated IPv4 packet")
        ihl = (packet[0] & 0x0F) * 4
        total_length = int.from_bytes(packet[2:4], "big")
        if ihl < 20 or total_length != len(packet):
            raise ValueError("invalid IPv4 header length")
        import socket

        return socket.inet_ntop(socket.AF_INET, packet[12:16]), socket.inet_ntop(
            socket.AF_INET, packet[16:20]
        )
    if version == 6:
        if len(packet) < 40:
            raise ValueError("truncated IPv6 packet")
        payload_length = int.from_bytes(packet[4:6], "big")
        if payload_length + 40 != len(packet):
            raise ValueError("invalid IPv6 payload length")
        import socket

        return socket.inet_ntop(socket.AF_INET6, packet[8:24]), socket.inet_ntop(
            socket.AF_INET6, packet[24:40]
        )
    raise ValueError("unsupported IP version")

