"""Optional AGW configuration; importing the base SDK does not require MOQ."""

from dataclasses import dataclass
from ipaddress import IPv4Address


@dataclass(frozen=True, slots=True)
class AGWConfig:
    relay_ipv4: str
    relay_port: int = 4433
    recovery_timeout: float = 30.0
    local_timeout: float = 5.0

    def __post_init__(self):
        IPv4Address(self.relay_ipv4)
        if type(self.relay_port) is not int or not 1 <= self.relay_port <= 65535:
            raise ValueError("invalid Relay UDP port")
        if not 0 < self.local_timeout <= self.recovery_timeout:
            raise ValueError("invalid AGW timeout configuration")
