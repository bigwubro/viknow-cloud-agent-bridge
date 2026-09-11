# 236 主机 Docker 操作审计方案

## 背景

2026-09-11 13:56 左右，`vllm-vlm` 被 Docker API `DELETE /containers/vllm-vlm` 强制删除（等效 `docker rm -f`），容器以 exit 137 退出。`journalctl -u docker` 能证明 **发生了 DELETE**，但 **不能记录是哪个 Unix 账号**。

根因：`docker` 组成员可直接访问 `/var/run/docker.sock`，**不经过 sudo**，因此 `auth.log` 里没有记录。当前 236 主机 **未安装 auditd**。

## 目标

以后再发生 `docker rm` / `docker stop` / API DELETE 时，能查到：

| 字段 | 来源 |
|------|------|
| Unix UID / 用户名 | auditd `SYSCALL` / `execve` |
| 进程 PID / 父进程 | auditd |
| 完整命令行 | auditd `execve` 或 `proctitle` |
| 操作对象（容器名） | auditd 参数 + docker events 补充 |
| 时间戳 | auditd + journal |

## 硬性约束（236 生产环境）

**以下操作一律禁止**，否则可能导致全环境容器异常：

- 修改 `/etc/docker/daemon.json` 或 dockerd 启动参数
- `systemctl restart docker` / 重启 dockerd
- 安装 Docker authorization plugin
- 变更 `/var/run/docker.sock` 权限或 `docker` 组成员
- 替换 `/usr/bin/docker` 二进制或 CLI 插件

本方案 **只追加只读监控**，不触碰 Docker 自身配置。

## 推荐方案（三层，均不修改 Docker 配置）

### 层 1：auditd（核心，必须）

监控两类入口：

1. **`/usr/bin/docker` 执行** — 捕获 CLI（`docker rm -f vllm-vlm`）
2. **`/var/run/docker.sock` 写入** — 捕获 Python/curl/脚本直连 API（本次 DELETE 很可能是这条路径）

安装（需 root，一次性）：

```bash
# 在 236 上以 root 执行
sudo bash docs/ops/236-docker-audit/install-auditd.sh
```

### 层 2：docker events 旁路日志（补充，无需 root）

`docker events` **不能**给出调用者 UID，但能记录 **哪个容器** 在何时被 destroy/kill，与 auditd 时间线交叉验证。

以 `cursor-agent` 或专用账号部署 systemd user service：

```bash
# 以 cursor-agent 登录 236
mkdir -p ~/.config/systemd/user
cp docs/ops/236-docker-audit/docker-event-watcher.service ~/.config/systemd/user/
cp docs/ops/236-docker-audit/docker-event-watcher.sh ~/bin/   # 或任意可执行路径
# 编辑 service 里 ExecStart 路径
systemctl --user daemon-reload
systemctl --user enable --now docker-event-watcher.service
loginctl enable-linger cursor-agent   # 需 root，保证 logout 后仍运行
```

日志默认写入 `~/.local/log/viknow-docker-events.log`。

### 层 3：治理（长期，需单独变更窗口）

与 Docker 配置/权限相关的治理（缩 docker 组、sudo 包装脚本等）**不在本方案自动执行范围内**；若要做，必须单独评审，且仍禁止修改 daemon.json / 重启 dockerd。

## 事后查询手册

### auditd：谁执行了 docker / 谁连了 docker.sock

```bash
# 今天所有 docker CLI
sudo ausearch -k viknow_docker_exec -ts today --interpret

# 今天所有 docker.sock 访问（含 API DELETE）
sudo ausearch -k viknow_docker_sock -ts today --interpret

# 缩小到 vllm-vlm 相关（命令行或 proctitle 含关键字）
sudo ausearch -k viknow_docker_exec -ts 09/11/2026 13:50:00 -te 14:00:00 --interpret \
  | grep -E 'vllm-vlm|docker rm|DELETE'

# 按 UID 汇总
sudo ausearch -k viknow_docker_sock -ts today --raw \
  | awk '/uid=/ {print}' | sort | uniq -c
```

典型有效记录示例（安装后）：

```
type=SYSCALL msg=audit(...): arch=... syscall=execve success=yes exit=0
  a0=... a1=... a2=...
  exe="/usr/bin/docker" comm="docker"
  uid=1012 gid=1013 euid=1012 suid=1012 fsuid=1012 egid=1013 sgid=1013 fsgid=1013
  key="viknow_docker_exec"
type=EXECVE msg=audit(...): argc=4 a0="docker" a1="rm" a2="-f" a3="vllm-vlm"
type=PROCTITLE msg=audit(...): proctitle=646F636B657220726D202D6620766C6C6D2D766C6D
```

### journal：Docker 守护进程侧

```bash
sudo journalctl -u docker --since "2026-09-11 13:56:40" --until "2026-09-11 13:57:00" \
  | grep -E 'DELETE|kill|vllm-vlm'
```

### docker events 旁路日志

```bash
grep vllm-vlm ~/.local/log/viknow-docker-events.log
# 或 centralized: /var/log/viknow/docker-events.log（若 install 脚本选了系统级）
```

### 与 SSH 登录交叉

```bash
sudo grep "Accepted publickey" /var/log/auth.log | grep "Sep 11 13:5"
```

## 236 当前状态（2026-09-11 排查）

| 项目 | 状态 |
|------|------|
| auditd | **未安装** |
| docker.sock | `root:docker`，成员：dtong,twj,dev,netdata,wlk,ljs,cursor-agent,guoshiyu |
| cursor-agent sudo | 只读（journalctl/grep/awk/cat /var/log/find home） |
| DELETE 13:56:42 | journal 有，**无 UID** |

## 权限说明

- `install-auditd.sh` 需要 **root**（`cursor-agent` 无法自行安装）
- `docker-event-watcher` 只需 **docker 组** 成员即可部署
- 若希望 Cloud Agent 能查 audit 日志，需在 sudoers 增加：
  ```
  cursor-agent ALL=(ALL) NOPASSWD: /usr/sbin/ausearch -k viknow_docker_* *
  ```

## 验证安装

```bash
# root 安装 auditd 后，以普通 docker 组成员测试（勿对生产容器！）
docker run --rm --name audit-smoke-test alpine:true
docker rm -f audit-smoke-test   # 若容器已 --rm 则跳过

sudo ausearch -k viknow_docker_exec -ts recent --interpret | tail -20
sudo ausearch -k viknow_docker_sock -ts recent --interpret | tail -20
```

预期：两条记录均含 **你的 uid** 与 **完整 argv**。
