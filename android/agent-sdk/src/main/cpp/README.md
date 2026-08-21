# `libmasque_core.so` JNI contract

The Android northbound API, `VpnService`, routing, cache and lifecycle are
implemented in Kotlin. RFC 9484 QUIC packet I/O is provided by a separately
built native HTTP/3 implementation because Android's public Kotlin HTTP APIs do
not expose extended CONNECT plus HTTP Datagrams.

The ARM64 implementation is shipped under `src/main/jniLibs/arm64-v8a/` and its
source is in `android/native/masque_core`. It exports these instance methods on
`com/rayneo/agent/sdk/masque/NativeMasqueBridge`:

| Method | JNI descriptor | Ownership/result |
|---|---|---|
| `nativeStart` | `(ILjava/lang/String;Ljava/lang/String;Ljava/lang/String;Ljava/lang/String;ILjava/lang/String;)J` | On a non-zero result, takes ownership of the detached TUN fd after CONNECT-IP, ADDRESS_ASSIGN and ROUTE_ADVERTISEMENT are ready. On zero, Kotlin retains and closes it. The nullable second string is Authorization; the last string is the app-private identity directory. |
| `nativeReplaceTunFd` | `(JI)Z` | On `true`, owns the new fd and closes the old fd after the packet pump swaps. On `false`, ownership remains with Kotlin. |
| `nativeStop` | `(J)V` | Stops both pumps and closes the active TUN and QUIC descriptors. |

Before the native core connects its QUIC UDP socket it must invoke the bridge
instance method `protectQuicSocket(I)Z`. Failure is fatal: otherwise the QUIC
outer connection can be routed back into the VPN and recurse.

The core negotiates `:protocol=connect-ip`, `Capsule-Protocol: ?1` and HTTP
Datagrams through `connect-ip-go`; it validates the ADDRESS_ASSIGN address
against the Agent TUN CIDR derived from `GET /v1/ue/info`, waits for route advertisement,
preserves packet boundaries, and bounds packets by the configured MTU. This
internal-test build disables server certificate verification; the client
identity is generated and persisted by the core, so no certificate path is
part of the public Kotlin API.
