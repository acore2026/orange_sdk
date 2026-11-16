# connect-ip-go client subset

[![PkgGoDev](https://pkg.go.dev/badge/github.com/quic-go/connect-ip-go)](https://pkg.go.dev/github.com/quic-go/connect-ip-go)
[![Code Coverage](https://img.shields.io/codecov/c/github/quic-go/connect-ip-go/master.svg?style=flat-square)](https://codecov.io/gh/quic-go/connect-ip-go/)

This directory vendors the files required by the endpoint CONNECT-IP client from
[quic-go/connect-ip-go](https://github.com/quic-go/connect-ip-go). The upstream
project implements both client and proxy roles; this SDK intentionally excludes
the proxy/request-handler source because servers are outside the SDK delivery
boundary.

The retained client uses [quic-go](https://github.com/quic-go/quic-go) and RFC 9484.

At this point, it supports the following use cases:
* Remote Access VPN, see [Section 8.1 of RFC 9484](https://datatracker.ietf.org/doc/html/rfc9484#section-8.1)
* Site-to-Site VPN, see [Section 8.2 of RFC 9484](https://datatracker.ietf.org/doc/html/rfc9484#section-8.2)


## Release Policy

connect-ip-go always aims to support the latest two Go releases.

For upstream release and contribution policy, see the original repository.
