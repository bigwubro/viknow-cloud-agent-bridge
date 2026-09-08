# 阿里云操作台账

本文件记录 Cloud Agent / 跳板环境对 **阿里云**（ACK、ACR、RDS、Tair、OSS、RAM 等）的每一次操作与变更。不记 AWS。

最后更新：2026-09-08

## 怎么记

1. 每次操作（登录探测、创建/变更/删除资源、改白名单、换镜像、kubectl apply、改 DNS 等）都在本文 **倒序追加** 一条。
2. 标题用日期 + 一句话动作，例如 `2026-09-08 阿里云 ACR 登录探测`。
3. 条目至少包含：操作者环境、对象、动作、结果、回滚/残留。
4. **禁止写入密码、AccessKey、Secret、license、kubeconfig 原文。** 只写资源 ID、用户名、是否成功、错误类型。
5. 台账里的生产凭据仍以线上本地 `infrastructure-inventory` / `deploy.env` 为准，不要把那份文件抄进本仓库。

## 条目模板

```md
### YYYY-MM-DD <动作>

- 环境：Cloud Agent 跳板 / 236 / ACK online prod …
- 对象：<产品 + 资源 ID 或名称>
- 动作：<做了什么>
- 结果：成功 / 失败 / 部分成功
- 证据：命令退出码、API 返回摘要、控制台状态（无密钥）
- 残留：是否留下登录态、临时文件、未释放资源
```

---

## 变更记录（倒序）

### 2026-09-08 更正：台账范围是阿里云，不是 AWS

- 环境：GitHub 跳板仓 `viknow-cloud-agent-bridge`，分支 `cursor/cloud-ops-log-10dc`
- 对象：本文件、`AGENTS.md`、`README.md`
- 动作：去掉所有 AWS 表述；后续只记录阿里云（含 ACK / ACR / RDS / Tair / OSS / RAM）操作与变更
- 结果：成功
- 残留：无云资源变更

### 2026-09-08 建立本台账

- 环境：GitHub 跳板仓 `viknow-cloud-agent-bridge`，分支 `cursor/cloud-ops-log-10dc`
- 对象：本文件 `docs/cloud-ops-log.md`；`AGENTS.md` 增加「每次阿里云操作必须追加」约定
- 动作：把同日阿里云登录探测写入台账，并规定后续阿里云操作都记在这里
- 结果：成功
- 残留：无云资源变更

### 2026-09-08 阿里云登录探测（ACR 成功，控制台卡 MFA，无 AK）

- 环境：Cursor Cloud Agent 跳板 VM + SSH `cursor-agent@36.103.198.236`
- 对象：
  - ACR 个人版华东1（杭州）`registry.cn-hangzhou.aliyuncs.com` / 命名空间 `litesense`
  - RAM 子账号 `zihan.wu`（主账号 `1239182347378119.onaliyun.com`）
  - ACK `viknow2-online-prod`（ClusterId `c0790f80e575446b796a1fbdbf1d8d0d3`）— 未实际连上
- 动作：用线上台账给出的 ACR/RAM 登录名探测能否登录阿里云；**未创建、修改、删除任何云资源**；未发送 MFA 短信
- 结果：部分成功

#### SSH 跳板

- Secret `VIKNOW_236_SSH_KEY` 落地后是单行 PEM，`libcrypto` 读失败。按 70 列重排换行后 `ssh-keygen -y` 成功。
- `ssh cursor-agent@36.103.198.236`：成功（`SSH_OK`，主机 `36-103-198-236`）。
- 容器 `viknow2-test-dev` 在跑。236 上 `cursor-agent` 的业务仓在 `/home/cursor-agent/work/viknow2`（当时分支 `agent/app-userid-ensure`），**不是**容器内 `/workspace`。

#### ACR Docker / Registry API

| 登录名 | 动作 | 结果 |
| --- | --- | --- |
| `zihan.wu@litesense` | `docker login registry.cn-hangzhou.aliyuncs.com`（在 236） | `Login Succeeded`（exit 0） |
| `zihan.wu@1239182347378119.onaliyun.com` | 同上密码再 login | `unauthorized` |
| `zihan.wu@litesense` | Cloud Agent VM 调 `dockerauth.cn-hangzhou.aliyuncs.com` 换 token，再 `GET /v2/litesense/viknow2-app/tags/list` | token 有效 |

`litesense/viknow2-app` 当时可见 tag：

- `415b56ede10b`
- `8d455a1b6142`
- `a0c964696168`（与线上台账「已推送」一致）
- `init`

测完已在 236 执行 `docker logout registry.cn-hangzhou.aliyuncs.com`，Docker `auths` 清空，未把密码留在 `~/.docker/config.json`。

#### RAM 控制台

- URL：`https://signin.aliyun.com/1239182347378119.onaliyun.com/login.htm`
- 用户名被识别（页面 `Hi zihan.wu`）。
- 密码未被拒绝。
- 随后「unusual logon」，要求验证手机 `159****8592`。未点「获取验证码」，浏览器关闭。
- 未进入 `home.console.aliyun.com` / ACK 控制台 / ACR 控制台。

#### ACK / OpenAPI

- 台账与跳板仓都没有 AccessKeyId / AccessKeySecret。
- 236 上无 `aliyun` CLI。
- `kubectl` 存在但 `current-context is not set`，没有 ACK kubeconfig，连不上 `viknow2-online-prod`。
- 236 上 `config/online/deploy.env` 不存在（只有空的 `deploy.env.example`），因此也拿不到 OSS AK。

#### 结论（给后续操作）

| 能力 | 现状 |
| --- | --- |
| ACR 推/拉（公网 Registry + `zihan.wu@litesense`） | 可用 |
| 阿里云控制台 | 密码有效，新来源 IP 要手机 MFA |
| ACK OpenAPI / `kubectl` | 缺 AK 与 kubeconfig，不可用 |
| 改 RDS / Tair / OSS / 节点池 | 同上，先不要当已登录控制台 |

要管线上 ACK，还需：RAM 子账号 AccessKey（或 STS）、ACK kubeconfig，或一次能过 MFA 的控制台会话。
