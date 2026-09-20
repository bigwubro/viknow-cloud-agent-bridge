忽略本跳板仓库的本地搭建/安装/测试。不要在本仓库实现业务功能。

**禁止直连** `36.103.198.236`。必须经跳板 `root@8.145.57.129`（见 `.cursor/skills/viknow-236-ssh-proxy/SKILL.md`）。

Secrets：`VIKNOW_236_SSH_KEY`、`VIKNOW_BASTION_SSH_PASSWORD`

```bash
mkdir -p ~/.ssh && chmod 700 ~/.ssh
printf '%s\n' "$VIKNOW_236_SSH_KEY" | sed '/./,$!d' > ~/.ssh/id_ed25519
chmod 600 ~/.ssh/id_ed25519
command -v sshpass >/dev/null || sudo apt-get install -y sshpass

ssh -i ~/.ssh/id_ed25519 -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new \
  -o ProxyCommand="sshpass -p \"$VIKNOW_BASTION_SSH_PASSWORD\" ssh -o StrictHostKeyChecking=no -W %h:%p root@8.145.57.129" \
  -p 22 cursor-agent@36.103.198.236 \
  'docker exec viknow2-test-dev bash -lc "
    cd /opt/viknow &&
    git fetch origin &&
    git checkout -B agent/cloud-smoke origin/main &&
    git commit --allow-empty -m \"chore: cloud agent smoke\" &&
    git push -u origin agent/cloud-smoke &&
    git log -1 --oneline &&
    git status -sb
  "'
```

汇报：SSH 是否成功、推送分支名、最新 commit。

禁止：直连 236；改 viknow2-test / private；推 main；把密钥写进任何仓库。
