# 服务端部署指南 (Docker)

完整流程：**首次部署 → 日常 git pull 更新 → 加 HTTPS → 故障排查**

---

## 0. 服务器要求

最低配置：**2 核 / 2 GB / 30 GB**，Ubuntu 22.04 / Debian 12 / CentOS Stream 9 任意。

- 境内或香港机房（akshare 上游是东方财富/腾讯/新浪，境外可能被限速）
- 安全组放行 22 / 80 / 443（如果不用域名，放行 8000）
- 设置时区 `Asia/Shanghai`（详见下面 step 2）

---

## 1. 装 Docker

```bash
# Ubuntu / Debian 一键脚本
curl -fsSL https://get.docker.com | sudo bash
sudo usermod -aG docker $USER     # 把当前用户加入 docker 组
newgrp docker                     # 立即生效 (不用退出重登)

docker --version
docker compose version
```

---

## 2. 设置时区（必做）

调度是按 `Asia/Shanghai` 走的，云服务器默认 UTC：

```bash
sudo timedatectl set-timezone Asia/Shanghai
date     # 看到 "CST 2026" 字样即正确
```

---

## 3. 拉代码

随便选个你有权限的目录,比如 `/root/zhang/` 或 `/opt/`:

```bash
# 例如:
cd /root/zhang
git clone https://github.com/phtrinhdong/agushare.git
cd agushare

# 或者:
sudo mkdir -p /opt && sudo chown $USER:$USER /opt
cd /opt
git clone https://github.com/phtrinhdong/agushare.git
cd agushare
```

后文用 `$AGUSHARE_DIR` 表示你的项目目录 (替换成你实际的路径,如 `/root/zhang/agushare` 或 `/opt/agushare`)。

所有数据会保存在 `$AGUSHARE_DIR/data/` 下,docker-compose 用相对路径挂载,所以**只要在项目目录下执行 docker 命令,数据就跟着这个目录走**。

如果是私有仓库，先配 SSH key：

```bash
ssh-keygen -t ed25519 -C "your@email"
cat ~/.ssh/id_ed25519.pub        # 复制结果, 贴到 GitHub → Settings → SSH keys
git clone git@github.com:phtrinhdong/agushare.git
```

---

## 4. 首次部署

```bash
cd $AGUSHARE_DIR

# 1) 复制并编辑环境变量 (用户名密码,公网部署务必填强密码)
cp .env.example .env
vi .env

# 2) (可选) 修改 config.yaml — 邮件、自选股、形态开关
vi ashare_agent/config.yaml

# 3) 一键部署
./deploy.sh init
```

`deploy.sh init` 会：
- 创建持久化目录 `data/cache/`、`data/output/`、`data/logs/`
- `docker compose up -d --build` 起两个容器：
  - **agushare-web** — FastAPI 看板，端口 8000
  - **agushare-scheduler** — 常驻进程，每交易日 15:35 自动扫描

访问 `http://你的服务器IP:8000`，输入 `.env` 里设的用户名密码即可看到看板。

---

## 数据持久化总览

所有运行时数据都挂出到宿主机 `$AGUSHARE_DIR/data/` 下，**docker rebuild / restart / down + up 都不会丢失**：

```
$AGUSHARE_DIR/data/
├── cache/                     # K线 parquet 短期缓存 (6h TTL)
├── output/
│   ├── reports/               # 扫描 Excel + JSON 快照 + 回测 HTML 报告
│   ├── charts/                # K线图 PNG
│   └── observations/          # K型回测观察结果 JSON
├── logs/                      # agent.log
└── agent_data/                # 长期数据 (新)
    ├── history/               # 历史 K线数据库 (~800 根/股, 用于回测观察)
    └── watchlist.json         # 特别关注列表 (含成本价/数量)
```

历史快照（每次扫描的 `signals_*.json`）永久保留在 `data/output/reports/` 里，最旧的可以追溯到部署第一天。

## ⚠️ 路径修正 (重要)

**早期版本的 docker-compose.yml 挂载路径有误**——挂载点是 `/app/cache` 等，但代码实际写入 `/app/ashare_agent/cache`。结果是：**容器层有数据，但宿主机看到的 `data/` 目录是空的，重建镜像后数据丢失**。

新版本已修正路径。如果你之前**已经在跑老版本**，做这次 `./deploy.sh update` 时：

1. **容器内已有的扫描历史、缓存会丢**（但通常不重要，重新扫描会快速补齐）
2. **建议**：先把容器内的有用数据 copy 出来：

   ```bash
   # 在 update 之前先备份
   docker compose exec web tar czf /tmp/agushare_backup.tgz \
       /app/ashare_agent/cache /app/ashare_agent/output \
       /app/ashare_agent/data 2>/dev/null || true
   docker cp agushare-web:/tmp/agushare_backup.tgz ./
   
   # 然后 update
   ./deploy.sh update
   
   # 把数据恢复到宿主机 (展开到正确目录)
   tar xzf agushare_backup.tgz -C ./data --strip-components=2 \
       app/ashare_agent/cache app/ashare_agent/output app/ashare_agent/data 2>/dev/null || true
   ```

如果不在意之前的扫描历史，可以直接 `./deploy.sh update`，后续数据会正确持久化。

## 5. 日常更新（git pull → 重新部署）

代码改动后，在**服务器上**：

```bash
cd $AGUSHARE_DIR
./deploy.sh update
```

等价于：

```bash
git pull --ff-only         # 拉新代码
docker compose build        # 重建镜像
docker compose up -d        # 平滑替换 (老容器停,新容器启,几乎无感)
```

如果 Dockerfile / requirements.txt 没改，`build` 会命中 pip 层缓存，几秒就完事。
只改了 Python 代码、Web 前端、自定义形态 → 同样几秒就好。

**只改 `config.yaml` 或新增 `patterns/custom/*.py`** 都是挂载进容器的，根本不用 build，只要：

```bash
docker compose restart web scheduler
```

---

## 6. 常用运维命令

```bash
./deploy.sh ps            # 看容器状态
./deploy.sh logs          # 看 web 实时日志
./deploy.sh logs scheduler # 看调度容器日志
./deploy.sh scan          # 立即跑一次扫描 (一次性容器,不影响主服务)
./deploy.sh down          # 停掉所有服务

# 进入容器排查
docker exec -it agushare-web bash

# 备份扫描结果
tar czf backup-$(date +%F).tar.gz data/output/

# 看磁盘占用
du -sh data/*
```

---

## 7. 加 HTTPS（推荐, 有域名时）

最省事的方案：**Caddy 自动 Let's Encrypt**。已经准备好了 `deploy/Caddyfile` 和 `deploy/docker-compose.caddy.yml`。

**步骤**：

1. DNS：在域名后台把 `your-domain.com` 的 A 记录指向服务器 IP，等 DNS 生效（`ping your-domain.com` 能 ping 到服务器 IP）。
2. 改域名：

   ```bash
   vi deploy/Caddyfile     # 把 your-domain.com 改成你的真实域名
   ```

3. 启动叠加 compose：

   ```bash
   docker compose \
     -f docker-compose.yml \
     -f deploy/docker-compose.caddy.yml \
     up -d --build
   ```

Caddy 会自动申请证书、自动续期，访问 `https://your-domain.com` 即可。

---

## 8. 邮件推送

把 `ashare_agent/config.yaml` 里的 `email.enabled` 改成 `true`，填好 SMTP 信息：

```yaml
email:
  enabled: true
  smtp_server: smtp.qq.com
  smtp_port: 465
  use_ssl: true
  sender: your_email@qq.com
  password: 你的SMTP授权码    # 不是 QQ 登录密码!
  receivers:
    - your_email@qq.com
```

改完执行 `docker compose restart scheduler` 即可。

**坑**：阿里云/腾讯云**默认封禁 25 端口出站**，建议用 465 (SSL)。

> 注意：`config.yaml` 在仓库里。如果你**修改后包含真实密码**，又用 `git push` 推上去，密码就泄露了。两种保护方式：
> - 在服务器上把 `config.yaml` 加进本地 `.git/info/exclude`，让本地的修改不会被 commit
> - 或者改用环境变量读密码（需要小改 `reporter.py`），把密码放进 `.env`

---

## 9. 故障排查

| 现象 | 排查 |
|------|------|
| `http://ip:8000` 打不开 | `./deploy.sh ps` 看 web 状态；`docker compose logs web` 看错误 |
| 扫描完全没结果 | 多半是 akshare 网络不通：`docker exec -it agushare-web curl -I https://www.eastmoney.com/` |
| 邮件发不出 | 端口 25 被封 → 用 465 SSL；授权码错 → 不是登录密码 |
| 扫描时间不对 | 检查 `date` 是否 CST；`docker exec agushare-scheduler date` 也应是 CST |
| 容器频繁重启 | `docker compose logs scheduler` 看异常堆栈；多半是 config.yaml 格式错 |
| 磁盘满 | `data/cache/` 过大可清空（重新拉数据），`data/output/reports/` 是历史档案,可归档后清理 |

---

## 10. 升级清单（每次发版前自查）

- [ ] `git pull` 在干净的工作区跑通（无冲突）
- [ ] `./deploy.sh update` 后 `docker compose ps` 全部 healthy
- [ ] 浏览器打开看板正常，能看到上次扫描结果
- [ ] 点击「触发扫描」能跑完，状态栏从 running → done
- [ ] 等下一个交易日 15:35，`docker compose logs scheduler` 应看到自动扫描日志
