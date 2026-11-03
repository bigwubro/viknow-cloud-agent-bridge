# 207 / Tailscale — Cloud Agent 入网凭据

**用途**：Cloud Agent VM 执行 `scripts/tailscale-connect-207.sh` 时使用的 Tailscale **install auth key**（与 207 节点 `qx-ai-07` 入网约 key 同源，可复用至密钥轮换前）。

## Install auth key

```text
tskey-auth-kH1szLNxQo11CNTRL-vhhZLo8UZTKBYudv5VFDTKfCSY8ZTyfK
```

## 用法

```bash
export VIKNOW_TAILSCALE_AUTH_KEY="$(grep -o 'tskey-auth-[^[:space:]`]*' docs/207-tailscale-auth.md | head -1)"
bash scripts/tailscale-connect-207.sh
```

207 节点侧（已在 QX-AI-07 配置 subnet router 时）：

```bash
sudo tailscale up --accept-routes \
  --auth-key=tskey-auth-kH1szLNxQo11CNTRL-vhhZLo8UZTKBYudv5VFDTKfCSY8ZTyfK \
  --hostname=qx-ai-07 \
  --advertise-routes=10.168.1.0/24
```

## 节点

| 主机名 | Tailscale IPv4 | 内网 |
|--------|----------------|------|
| `qx-ai-07` | `100.101.46.52` | `10.168.1.207` |

Cloud Agent SSH：`wuzihan@100.101.46.52`（见 `scripts/ssh-207.sh`）。
