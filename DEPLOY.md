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

```bash
sudo mkdir -p /opt && cd /opt
sudo chown $USER:$USER /opt

git clone https://github.com/phtrinhdong/agushare.git
cd agushare
```

如果是私有仓库，先配 SSH key：

```bash
ssh-keygen -t ed25519 -C "your@email"
cat ~/.ssh/id_ed25519.pub        # 复制结果, 贴到 GitHub → Settings → SSH keys
git clone git@github.com:phtrinhdong/agushare.git
```

---

## 4. 首次部署

```bash
cd /opt/agushare

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

## 5. 日常更新（git pull → 重新部署）

代码改动后，在**服务器上**：

```bash
cd /opt/agushare
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
