#!/usr/bin/env bash
# =========================================================
# agushare — 部署 / 更新脚本
# 用法:
#   ./deploy.sh init    # 首次部署
#   ./deploy.sh update  # 拉新代码 + 重建并平滑替换
#   ./deploy.sh logs    # 看 web 日志
#   ./deploy.sh scan    # 立即跑一次扫描 (一次性容器)
#   ./deploy.sh ps      # 看容器状态
#   ./deploy.sh down    # 停止服务
# =========================================================
set -euo pipefail

cmd=${1:-update}

cd "$(dirname "$0")"

case "$cmd" in
  init)
    [ -f .env ] || cp .env.example .env
    echo "已生成 .env,记得编辑里面的 AGU_AUTH_PASS 等敏感字段后再继续。"
    mkdir -p data/cache data/output/reports data/output/charts data/logs
    docker compose up -d --build
    docker compose ps
    ;;

  update)
    echo ">>> git pull"
    git pull --ff-only
    echo ">>> docker compose build"
    docker compose build
    echo ">>> docker compose up -d (滚动替换)"
    docker compose up -d
    docker compose ps
    ;;

  logs)
    docker compose logs -f --tail=200 "${2:-web}"
    ;;

  scan)
    # 立即扫一次 (跑完容器自动销毁,不影响主服务)
    docker compose run --rm web python -m ashare_agent.main run --force
    ;;

  ps)
    docker compose ps
    ;;

  down)
    docker compose down
    ;;

  *)
    echo "未知命令: $cmd"
    grep '^  [a-z]\+)' "$0" | sed 's/)//'
    exit 1
    ;;
esac
