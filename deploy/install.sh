#!/usr/bin/env bash
# 服务器端安装脚本：daily-brief -> a-stock.chrisite.eu.cc
#
# 幂等：可重复执行（git 仓库存在则更新，venv 存在则跳过创建）
# 依赖：git、python3-venv、nginx（均已具备）
#
# 用法：bash deploy/install.sh [git_url]
set -euo pipefail

REPO_URL="${1:-https://github.com/SuCriss/daily-brief.git}"
APP_DIR=/opt/daily-brief
DATA_DIR="$APP_DIR/data"
VENV="$APP_DIR/venv"

echo "=== 1) 代码：clone / 更新 ==="
if [ -d "$APP_DIR/.git" ]; then
    git -C "$APP_DIR" fetch --depth=1 origin main
    git -C "$APP_DIR" reset --hard origin/main
else
    mkdir -p "$APP_DIR"
    git clone --depth=1 "$REPO_URL" "$APP_DIR"
fi
cd "$APP_DIR"
git log --oneline -1

echo
echo "=== 2) 数据目录 ==="
mkdir -p "$DATA_DIR"

echo
echo "=== 3) Python 虚拟环境 + 依赖 ==="
if [ ! -x "$VENV/bin/python" ]; then
    python3 -m venv "$VENV"
fi
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet -r requirements.txt
"$VENV/bin/python" -c "import fastapi, uvicorn, requests, jinja2; print('deps ok')"

echo
echo "=== 4) 初始化数据库与字典 ==="
DAILY_BRIEF_DB="$DATA_DIR/brief.db" "$VENV/bin/python" cli.py init
DAILY_BRIEF_DB="$DATA_DIR/brief.db" "$VENV/bin/python" - <<'PY'
import db, extract
db.init_db()
print("stocks:", extract.load_stock_dict(force=True))
print("sectors:", extract.load_sector_dict())
PY

echo
echo "=== 5) systemd 单元 ==="
install -m 644 deploy/daily-brief-web.service       /etc/systemd/system/daily-brief-web.service
install -m 644 deploy/daily-brief-scheduler.service /etc/systemd/system/daily-brief-scheduler.service
systemctl daemon-reload
systemctl enable --now daily-brief-web.service daily-brief-scheduler.service

echo
echo "=== 6) nginx 站点 ==="
install -m 644 deploy/a-stock.chrisite.eu.cc.conf /etc/nginx/conf.d/a-stock.chrisite.eu.cc.conf
nginx -t
systemctl reload nginx

echo
echo "=== 7) 状态 ==="
sleep 3
systemctl is-active daily-brief-web.service daily-brief-scheduler.service
ss -tlnp | grep 8848 || true
echo
echo "安装完成。"
