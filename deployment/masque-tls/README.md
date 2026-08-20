# MASQUE 服务端 TLS 部署材料

这里的证书供当前封闭实验网的 MASQUE Server 使用。对应的
`masque-server-key.pem` 只在当前安全工作区/交付介质中保存，并由 `.gitignore`
排除，必须通过受控渠道交给服务器运维；远端源码、客户端 Wheel 和 Android
AAR **均不包含服务端私钥**。

现有 Go Server 无需修改代码，只需在其既有配置中设置：

```bash
sudo install -m 0644 masque-server-cert.pem /etc/agent-sdk/
sudo install -m 0600 masque-server-key.pem /etc/agent-sdk/
```

```yaml
connectIP:
  tlsCertFile: '/absolute/path/to/masque-server-cert.pem'
  tlsKeyFile: '/absolute/path/to/masque-server-key.pem'
```

证书的 TLS 名称为 `masque.agent.internal`。当前封闭内测版 SDK 已关闭 MASQUE
服务端证书链和名称校验，因此它也能连接其他自签名服务端证书；客户调用
`sdk.init`/`initialize` 时不传证书、私钥或 Server Name。

本证书用于 POC/封闭测试。新克隆的源码仓库不会取得私钥，缺少安全交付的
`masque-server-key.pem` 时不能启用该证书。关闭校验仅用于内部测试；生产部署
必须恢复证书验证，并使用组织 CA 签发的服务端证书。
