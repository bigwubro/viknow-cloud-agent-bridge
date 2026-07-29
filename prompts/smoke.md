忽略本跳板仓库的本地搭建/安装/测试。不要在本仓库实现业务功能。

用 Secret `VIKNOW_236_SSH_KEY` 连 236 并冒烟：

1. mkdir -p ~/.ssh && chmod 700 ~/.ssh
2. 若 Secret 是私钥原文：
   printf '%s\n' "$VIKNOW_236_SSH_KEY" > ~/.ssh/id_ed25519
   若是 base64：
   echo "$VIKNOW_236_SSH_KEY" | base64 -d > ~/.ssh/id_ed25519
3. chmod 600 ~/.ssh/id_ed25519
4. ssh -i ~/.ssh/id_ed25519 -o StrictHostKeyChecking=accept-new cursor-agent@36.103.198.236 \
     'docker exec viknow2-test-dev bash -lc "
       cd /workspace &&
       git fetch origin &&
       git checkout -B agent/cloud-smoke origin/main &&
       git commit --allow-empty -m \"chore: cloud agent smoke\" &&
       git push -u origin agent/cloud-smoke &&
       git log -1 --oneline &&
       git status -sb
     "'
5. 汇报：SSH 是否成功、推送分支名、最新 commit。

禁止：改 viknow2-test / private；推 main；把密钥写进任何仓库。
