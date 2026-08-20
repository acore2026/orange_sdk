module github.com/acore2026/orange-sdk/android/masque-core

go 1.25.0

require (
	github.com/quic-go/connect-ip-go v0.2.0
	github.com/quic-go/quic-go v0.61.0
	github.com/yosida95/uritemplate/v3 v3.0.2
	golang.org/x/sys v0.47.0
)

replace github.com/quic-go/connect-ip-go => ./third_party/connect-ip-go

require (
	github.com/dunglas/httpsfv v1.0.2 // indirect
	github.com/quic-go/qpack v0.6.0 // indirect
	golang.org/x/crypto v0.54.0 // indirect
	golang.org/x/net v0.56.0 // indirect
	golang.org/x/text v0.40.0 // indirect
)
