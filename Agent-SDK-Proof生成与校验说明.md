# Agent SDK Proof 生成与校验说明

| 文档属性 | 内容 |
|---|---|
| 文档版本 | V1.1 |
| 编制日期 | 2026-08-24 |
| 适用版本 | Python Wheel 0.14.0、Android/RayNeoOS SDK |
| 适用对象 | SDK 开发者、AgentRuntime 开发者、核心网开发者、联调测试人员 |

## 1. 说明范围

本文说明 Agent SDK 当前使用的消息级 `proof` 生成和校验方法，覆盖以下三类消息：

1. SDK 发往 AgentRuntime 的非身份申请控制面写请求以及 H-COMPUTE 算力卸载请求；
2. 核心网通过 AgentRuntime 下发的 `acf_group_config` 群组配置；
3. Agent 之间通过 `POST /A2A/message` 发送的定向消息。

本文不覆盖以下两类签名：

- `POST /idm/v1/identity-applications` 使用的 `signature`。该接口采用
  `ACN-H-ID-v1\0 + LP16/U64BE` 专用字段编码，其中完整紧凑
  `metadata` JSON Container 作为单个 LP16 字段，不使用本文的 `proof`
  算法。SDK 固定必填字段 `region/os/version` 的顺序，其他字符串字段
  按名称排序，保证签名字节与 HTTP/N-01 传输的 Metadata JSON 完全一致；
- 内测三方能力机构签发 Capability VC 时使用的 `creator + signature_value`。
  该测试 VC 使用另一套凭证签名格式，不属于本文的消息级 proof。

当前实现保留既有 snake_case 线上字段，并使用项目约定的排序紧凑 JSON
规范化。因此本文将其称为 **ACN JsonWebSignature2020 兼容 Profile**。它不是
采用 JSON-LD URDNA2015 和 camelCase 字段的完整 W3C JsonWebSignature2020
实现。生成方和校验方必须实现本文定义的相同字节规则，不能只根据
`proof.type` 猜测签名算法。

## 2. Proof 结构

SDK 生成的 proof 格式如下：

```json
{
  "type": "JsonWebSignature2020",
  "verification_method": "did:key:zDnaExampleP256#zDnaExampleP256",
  "proof_purpose": "authentication",
  "created": "2026-08-21T00:00:00.000Z",
  "jws": "eyJhbGciOiJFUzI1NiIsImI2NCI6ZmFsc2UsImNyaXQiOlsiYjY0Il19..BASE64URL_OF_64_BYTE_R_S"
}
```

| 字段 | 类型 | 生成规则 | 当前校验规则 |
|---|---|---|---|
| `type` | string | 固定为 `JsonWebSignature2020` | 必须完全匹配 |
| `verification_method` | string | 本机 P-256 `did:key` 加同一 key fragment | 字段受签名保护；当前基础验签函数不根据它选择公钥，也不单独校验其格式 |
| `proof_purpose` | string | 按使用场景填 `authentication` 或 `assertionMethod` | 必须与调用方给出的预期用途完全匹配 |
| `created` | string | SDK 生成 UTC 时间 | 当前基础验签函数只要求为非空字符串；时间窗由上层协议另行处理 |
| `jws` | string | ES256、RFC 7797 `b64=false` 分离 JWS | 必须存在；主格式必须满足第 5 章规则 |

字段名不得改成 `verificationMethod` 或 `proofPurpose`，字段大小写不得改变。

## 3. 密钥和信任来源

### 3.1 端侧设备私钥

- Linux/Python：首次初始化生成 P-256 私钥并保存在
  `$XDG_STATE_HOME/agent-sdk/security/device-private-key.pem`；未配置
  `XDG_STATE_HOME` 时使用 `~/.local/state/agent-sdk/security/`；
- Android/RayNeoOS：首次初始化在 Android Keystore 中生成 P-256 私钥，alias
  为 `agent-sdk-device-signing-v1`，私钥不可导出；
- 同一设备后续启动复用同一密钥，不为每条消息生成新密钥。

### 3.2 验签公钥的选择

验签公钥必须由 proof 外部的可信上下文预先选定：

| 消息 | `proof_purpose` | 验签公钥来源 |
|---|---|---|
| SDK 控制面上行请求 | `authentication` | 网侧根据已经登记的 Agent/设备身份取得端侧公钥 |
| `acf_group_config` | `assertionMethod` | SDK 内置的核心网 P-256 公钥 |
| A2A 定向消息 | `authentication` | 已通过核心网 proof 校验并提交的群组快照中，发送方成员的 `did_key` |

不得直接读取未验签消息里的 `verification_method`，然后用它指定的公钥验证同一
条消息。`verification_method` 是受签名保护的声明字段，不是当前 SDK 的信任锚。

## 4. 待签数据生成

### 4.1 构造业务文档

不同消息的待签业务文档如下：

- 非身份申请控制请求和 H-COMPUTE：HTTP 请求体去掉 `request_id`，加入 SDK 生成的
  `timestamp`，并且不包含 `proof`；
- A2A 消息：包含 `message_id/group_id/type/timestamp/payload/src_agent_id/
  dst_agent_id/task_id`，不包含 `proof`；
- 群组配置：包含核心网生成的完整群组配置业务字段，不包含 `proof`。

控制请求的 `request_id` 只用于 HTTP 幂等、事务关联和重试，不进入业务签名。
A2A 的 `message_id` 是业务消息字段，需要进入业务文档摘要。

### 4.2 构造 proofOptions

生成阶段先构造不含 `jws` 的 proof：

```json
{
  "type": "JsonWebSignature2020",
  "verification_method": "did:key:zDnaExampleP256#zDnaExampleP256",
  "proof_purpose": "authentication",
  "created": "2026-08-21T00:00:00.000Z"
}
```

校验阶段使用收到的完整 `proof` 复制出 `proofOptions`，然后只删除 `jws`。
未知 proof 元数据不会被静默删除；只要它保留在 `proofOptions` 中，就会参与摘要。

### 4.3 JSON 规范化

业务文档和 proofOptions 分开执行相同的 `canonical_json`：

1. JSON 对象的键在每一层递归升序排列；
2. JSON 数组保持原顺序；
3. 不输出缩进、换行或冒号和逗号周围的空格；
4. 输出 UTF-8 字节，非 ASCII 业务字符串不强制转换成 `\uXXXX`；
5. 字符串按 JSON 规则转义。

示例：

```json
{
  "timestamp": "2026-08-21T00:00:00Z",
  "agent_id": "did:example:a",
  "intent": "Issue Network Ability Credential"
}
```

规范化后为以下连续 UTF-8 字节：

```text
{"agent_id":"did:example:a","intent":"Issue Network Ability Credential","timestamp":"2026-08-21T00:00:00Z"}
```

当前算法不是完整 RFC 8785 JCS。跨语言实现必须使用相同 JSON 数据模型和序列化
规则。协议字段名均为 ASCII；A2A 自定义 `payload` 为避免不同语言对浮点数和
指数形式重新序列化产生差异，跨平台消息应优先使用字符串、整数、布尔值、
`null`、对象和数组，不建议在签名字段中直接使用浮点数。

### 4.4 计算双摘要

先计算 proofOptions 摘要，再计算不含 proof 的业务文档摘要：

```text
proofHash = SHA-256(canonical_json(proofOptions))
documentHash = SHA-256(canonical_json(unsecuredDocument))
verifyData = proofHash || documentHash
```

其中：

- `proofHash` 固定 32 字节；
- `documentHash` 固定 32 字节；
- `verifyData` 固定 64 字节；
- 拼接顺序固定为 proof 摘要在前、业务文档摘要在后，不能交换；
- `||` 表示原始字节直接拼接，不是十六进制字符串拼接，也不添加分隔符。

## 5. JWS 生成

### 5.1 Protected Header

Protected Header 固定为：

```json
{
  "alg": "ES256",
  "b64": false,
  "crit": ["b64"]
}
```

规范化后得到：

```text
{"alg":"ES256","b64":false,"crit":["b64"]}
```

Base64URL 编码且去掉 `=` padding 后为：

```text
eyJhbGciOiJFUzI1NiIsImI2NCI6ZmFsc2UsImNyaXQiOlsiYjY0Il19
```

### 5.2 签名输入

JWS 使用 RFC 7797 未编码载荷，签名输入为：

```text
signingInput = ASCII(BASE64URL(protectedHeader)) || 0x2E || verifyData
```

`0x2E` 是一个 ASCII 句点 `.`。这里拼入的是 64 字节原始 `verifyData`，不是其
Base64URL、十六进制或 JSON 表示。

### 5.3 ES256 签名和输出

使用 P-256 ECDSA + SHA-256 对 `signingInput` 签名。密码库通常返回 ASN.1 DER
编码的 `(r, s)`，SDK 将其转换为 JWS 要求的固定 64 字节：

```text
rawSignature = I2OSP(r, 32) || I2OSP(s, 32)
jws = protected || ".." || BASE64URL(rawSignature)
```

两个连续句点表示 JWS payload 不写入 JSON 字符串；校验方根据收到的业务文档和
proofOptions 重建同一个 `verifyData`。

完整生成伪代码：

```text
function create_proof(unsecuredDocument, privateKey, purpose, verificationMethod):
    proofOptions = {
        "type": "JsonWebSignature2020",
        "verification_method": verificationMethod,
        "proof_purpose": purpose,
        "created": utc_now()
    }

    proofHash = SHA256(canonical_json(proofOptions))
    documentHash = SHA256(canonical_json(unsecuredDocument without proof))
    verifyData = proofHash || documentHash

    header = {"alg":"ES256", "b64":false, "crit":["b64"]}
    protected = BASE64URL(canonical_json(header), no_padding=true)
    signingInput = ASCII(protected) || "." || verifyData
    derSignature = ECDSA_P256_SHA256_SIGN(privateKey, signingInput)
    rawSignature = DER_TO_JOSE_RS(derSignature, 32, 32)

    proof = proofOptions
    proof.jws = protected || ".." || BASE64URL(rawSignature, no_padding=true)
    return proof
```

## 6. Proof 校验

### 6.1 主格式校验步骤

当前 Python 和 Android SDK 按以下顺序校验：

1. 从消息顶层取得 `proof`，要求它是 JSON 对象；
2. 要求 `proof.type == "JsonWebSignature2020"`；
3. 要求 `proof.proof_purpose` 等于调用场景预期值；
4. 要求 `proof.created` 是非空字符串；
5. 要求 `proof.jws` 是非空字符串；
6. 从完整 proof 删除 `jws`，得到 `proofOptions`；
7. 从完整消息删除整个 `proof`，得到 `unsecuredDocument`；
8. 按第 4 章重新计算 64 字节 `verifyData`；
9. 拆分 `jws`，要求正好有三个部分并且中间 payload 部分为空；
10. Base64URL 解码 Protected Header，要求 `alg=ES256`、`b64=false`、
    `crit` 精确等于 `["b64"]`；
11. Base64URL 解码 JWS 签名，要求结果恰好 64 字节；
12. 将前 32 字节解释为 `r`，后 32 字节解释为 `s`，转换成密码库需要的 DER；
13. 使用调用方预先选择的 P-256 公钥验证
    `ASCII(protected) || "." || verifyData`；
14. 任一步失败均返回 SDK 的 `SIGNATURE_ERROR`。

校验伪代码：

```text
function verify_proof(securedDocument, trustedPublicKey, expectedPurpose):
    proof = require_object(securedDocument.proof)
    require proof.type == "JsonWebSignature2020"
    require proof.proof_purpose == expectedPurpose
    require non_empty_string(proof.created)
    require non_empty_string(proof.jws)

    proofOptions = copy(proof) without jws
    unsecuredDocument = copy(securedDocument) without proof
    verifyData =
        SHA256(canonical_json(proofOptions)) ||
        SHA256(canonical_json(unsecuredDocument))

    protected, detachedPayload, encodedSignature = split_jws(proof.jws)
    require detachedPayload == ""
    header = BASE64URL_DECODE_JSON(protected)
    require header == compatible({"alg":"ES256","b64":false,"crit":["b64"]})
    rawSignature = BASE64URL_DECODE(encodedSignature)
    require length(rawSignature) == 64

    signingInput = ASCII(protected) || "." || verifyData
    return ECDSA_P256_SHA256_VERIFY(
        trustedPublicKey,
        signingInput,
        JOSE_RS_TO_DER(rawSignature)
    )
```

### 6.2 当前兼容校验分支

当前 Python 和 Android 验签实现还保留一个历史兼容分支：如果 `jws` 不包含两个
句点，则把整个 `jws` 当作标准 Base64 编码的 ASN.1 DER ECDSA 签名，并直接对
64 字节 `verifyData` 执行 P-256/SHA-256 验签。

SDK 自己不会生成这种格式。新网元和新 Agent 必须生成第 5 章规定的分离 JWS；
历史兼容分支只用于读取旧消息，不能作为新协议实现依据。

## 7. 跨平台黄金向量

Python 和 Android 测试使用以下 proofOptions：

```json
{
  "type": "JsonWebSignature2020",
  "verification_method": "did:key:zExample#zExample",
  "proof_purpose": "authentication",
  "created": "2026-08-21T00:00:00Z"
}
```

规范化结果：

```text
{"created":"2026-08-21T00:00:00Z","proof_purpose":"authentication","type":"JsonWebSignature2020","verification_method":"did:key:zExample#zExample"}
```

`proofHash`：

```text
1a96f0c94b92eaa51b8fb1de55b1842584e66a24be9af373507bd956581ab0b3
```

业务文档：

```json
{
  "agent_id": "did:example:a",
  "intent": "Issue Network Ability Credential",
  "timestamp": "2026-08-21T00:00:00Z"
}
```

规范化结果：

```text
{"agent_id":"did:example:a","intent":"Issue Network Ability Credential","timestamp":"2026-08-21T00:00:00Z"}
```

`documentHash`：

```text
31126a50a843b70e3b740f33884f6d0dc38054a942753600f9546c10a67122c1
```

最终 64 字节 `verifyData` 的十六进制表示：

```text
1a96f0c94b92eaa51b8fb1de55b1842584e66a24be9af373507bd956581ab0b331126a50a843b70e3b740f33884f6d0dc38054a942753600f9546c10a67122c1
```

接入方可以先实现该向量。如果任一规范化结果或摘要不一致，不要继续联调网络
签名，因为后续生成的所有 JWS 都无法互相验证。

## 8. 三类消息的具体处理

### 8.1 控制面上行请求

以获取运营商网络能力凭证为例，最终 HTTP 请求为：

```json
{
  "request_id": "9e4b0db9-450a-43a7-bda2-a539885f25be",
  "agent_id": "did:example:a",
  "intent": "Issue Network Ability Credential",
  "timestamp": "2026-08-21T00:00:00Z",
  "proof": {
    "type": "JsonWebSignature2020",
    "verification_method": "did:key:zDnaExampleP256#zDnaExampleP256",
    "proof_purpose": "authentication",
    "created": "2026-08-21T00:00:00Z",
    "jws": "eyJhbGciOiJFUzI1NiIsImI2NCI6ZmFsc2UsImNyaXQiOlsiYjY0Il19..BASE64URL_OF_64_BYTE_R_S"
  }
}
```

实际计算 `documentHash` 时使用：

```json
{
  "agent_id": "did:example:a",
  "intent": "Issue Network Ability Credential",
  "timestamp": "2026-08-21T00:00:00Z"
}
```

即同时排除 HTTP `request_id` 和完整 `proof`。AgentRuntime 只能透传，不应删除、
增加、改名或重建任何业务字段、proof 字段和 JWS 字符串。

### 8.2 核心网群组配置

SDK 收到 `ACN_AGENT_GROUPING_NOTIFICATION` 后，先使用内置核心网公钥校验
`payload.proof`，预期 `proof_purpose=assertionMethod`。只有 proof 校验成功，
SDK 才解析成员、校验本机 Agent TUN IP 和端口、提交群组快照并安装动态路由。

群组成员里的 `did_key` 不能用于验证承载这些成员信息的同一条群组配置，否则会
形成消息自带公钥、自证消息有效的错误信任链。

### 8.3 A2A 定向消息

发送端先构造不含 proof 的完整 A2A 消息，使用本机设备私钥和
`proof_purpose=authentication` 生成 proof，随后发送到从已提交群组缓存解析出的
`agent_ip:tcp_port`。

接收端根据 `group_id + src_agent_id` 从已验签群组快照取得发送方 `did_key`，
解析为 P-256 公钥后校验 proof。验签通过后才把 `payload` 交给应用 listener。

## 9. 完整性范围和上层安全边界

当前 proof 可以保证参与双摘要的字段在签名后没有被修改，并证明签名者持有与
预选公钥对应的 P-256 私钥。但单独验证 proof 不等于完成全部业务安全校验：

- `verify_proof` 本身不判断签名者是否有权执行具体业务操作；调用方必须先确定
  可信公钥和预期 `proof_purpose`；
- `verify_proof` 当前只检查 `created` 非空，不解析其时区，也不检查与当前时间的
  最大偏差；
- 群组配置上层会解析业务 `timestamp`，并拒绝不比已提交快照更新的配置，但当前
  没有单独的绝对时间窗口；
- A2A 上层当前只要求业务 `timestamp` 非空，proof 基础验签函数不负责防重放；
- HTTP 控制请求的幂等和重试由 `request_id` 处理，而 `request_id` 按契约不进入
  业务签名。

如果部署要求严格防重放，应在不改变本文签名字节的前提下，额外实现时间窗口、
消息 ID/请求摘要缓存和幂等结果复用。

## 10. 常见对接错误

| 错误 | 后果 |
|---|---|
| 把 proofOptions 放回业务文档后整体规范化 | `documentHash` 不一致，验签失败 |
| 计算业务摘要时只删除 `jws`，但保留其他 proof 字段 | `documentHash` 不一致，验签失败 |
| 完全不签 proofOptions | proof 元数据可被篡改，且与当前 SDK 不兼容 |
| 使用 `documentHash || proofHash` | 摘要顺序错误，验签失败 |
| 把两个摘要的十六进制文本拼接 | 签名输入长度从 64 字节变成 128 字节，验签失败 |
| 对 `verifyData` 再做 Base64URL 后作为 JWS payload | 违反当前 `b64=false` 规则，验签失败 |
| JWS 中间段写入 payload | 当前 SDK 要求分离 JWS，中间段必须为空 |
| 直接把 DER ECDSA 签名放入分离 JWS 第三段 | 主格式要求 64 字节 `r || s`，验签失败 |
| 改成 `verificationMethod/proofPurpose` | proofHash 改变，并违反现有 HTTP/NAS 字段契约 |
| 控制请求把 `request_id` 加入 `documentHash` | 网元与 SDK 的签名输入不一致 |
| 根据未验签消息自己的 `verification_method` 选择公钥 | 形成自证明，无法建立可信身份 |

## 11. 实现位置和测试

Python：

- `python/src/agent_sdk/security.py::canonical_json`
- `python/src/agent_sdk/security.py::_proof_signing_bytes`
- `python/src/agent_sdk/security.py::DeviceSigningIdentity.create_proof`
- `python/src/agent_sdk/security.py::verify_proof`
- `python/tests/test_security.py`

Android：

- `android/agent-sdk/src/main/kotlin/com/rayneo/agent/sdk/security/AndroidDeviceSecurity.kt::canonicalJson`
- `AndroidDeviceSecurity.kt::proofSigningBytes`
- `AndroidDeviceSecurity.kt::createProof`
- `AndroidDeviceSecurity.kt::verifyProof`
- `android/agent-sdk/src/test/kotlin/com/rayneo/agent/sdk/security/AndroidDeviceSecurityTest.kt`

两端测试共同断言第 7 章的 64 字节黄金向量，并覆盖业务正文篡改、
`verification_method` 篡改、核心网固定公钥验签和 A2A 成员 `did_key` 验签。
