忽略本跳板仓库的本地搭建。不要在本仓库写业务代码。

阅读 AGENTS.md，然后：

1. 用 Secret `VIKNOW_236_SSH_KEY` 写入 `~/.ssh/id_ed25519` 并 chmod 600
2. SSH 到 `cursor-agent@36.103.198.236`
3. `docker exec -it viknow2-test-dev bash`（或非交互 `docker exec ... bash -lc`）
4. `cd /workspace`
5. `git fetch origin && git checkout -B agent/<topic> origin/main`
6. 完成任务：<在这里写具体需求>
7. 在 test-dev 冒烟（容器内 :8000 或宿主机 :5176）
8. commit 后 `git push -u origin agent/<topic>`
9. 汇报分支名、相对 origin/main 的 log/diff --stat

禁止：推 main；动 viknow2-test / private；把密钥写入提交。
