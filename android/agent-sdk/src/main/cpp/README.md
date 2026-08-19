# `libmasque_core.so` JNI contract

The Android northbound API, `VpnService`, routing, cache and lifecycle are
implemented in Kotlin. RFC 9484 QUIC packet I/O is provided by a separately
built native HTTP/3 implementation because Android's public Kotlin HTTP APIs do
not expose extended CONNECT plus HTTP Datagrams.

Package one `libmasque_core.so` under `src/main/jniLibs/<abi>/`. The library must
register these instance methods on
`com/rayneo/agent/sdk/masque/NativeMasqueBridge`:

| Method | JNI descriptor | Ownership/result |
|---|---|---|
| `nativeStart` | `(ILjava/lang/String;Ljava/lang/String;[BLjava/lang/String;Ljava/lang/String;Ljava/lang/String;I)J` | On a non-zero result, takes ownership of the detached TUN fd after CONNECT-IP is ready. On zero, Kotlin retains and closes it. The first nullable string after CA bytes is the Authorization header. |
| `nativeReplaceTunFd` | `(JI)Z` | On `true`, owns the new fd and closes the old fd after the packet pump swaps. On `false`, ownership remains with Kotlin. |
| `nativeStop` | `(J)V` | Stops both pumps and closes the active TUN and QUIC descriptors. |

Before the native core connects its QUIC UDP socket it must invoke the bridge
instance method `protectQuicSocket(I)Z`. Failure is fatal: otherwise the QUIC
outer connection can be routed back into the VPN and recurse.

The core must negotiate `:protocol=connect-ip`, `Capsule-Protocol: ?1` and HTTP
Datagrams; use Context ID 0 for complete IP packets, validate inner source and
destination policy, preserve packet boundaries, and reject packets larger than
the configured MTU.
