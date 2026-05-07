#!/usr/bin/env bash
# =============================================================================
# Tix · 一键部署 / 升级脚本（适配宝塔面板 / 任意 Linux 服务器）
#
# 仓库: https://github.com/linlea666/tix
#
# 使用:
#   首次部署:
#     curl -fsSL https://raw.githubusercontent.com/linlea666/tix/main/deploy.sh \
#       -o /www/wwwroot/tix/deploy.sh
#     bash /www/wwwroot/tix/deploy.sh install
#
#   或直接克隆仓库后:
#     bash /www/wwwroot/tix/deploy.sh install
#
# 命令:
#   install              首次部署：拉取仓库 + 构建镜像 + 启动
#   update               拉取最新代码 + 重新构建 + 重启
#   restart              重启容器
#   stop                 停止容器
#   start                启动已停止的容器
#   logs                 跟随日志
#   status               查看容器状态
#   shell                进入容器 shell
#   password 新密码      修改 admin 密码
#   port      新端口     修改 host 端口（写入 .env，重启生效）
#   uninstall            停止容器并删除镜像（保留 data 目录）
#
# 环境变量（可选）:
#   APP_DIR=/www/wwwroot/tix    部署目录
#   PORT=8688                   宿主机端口
#   REPO=https://github.com/linlea666/tix.git
# =============================================================================
set -euo pipefail

REPO="${REPO:-https://github.com/linlea666/tix.git}"
APP_DIR="${APP_DIR:-/www/wwwroot/tix}"
PORT="${PORT:-8688}"
CONTAINER="tix"

# ----- pretty -----
red()    { printf "\033[31m%s\033[0m\n" "$*"; }
green()  { printf "\033[32m%s\033[0m\n" "$*"; }
yellow() { printf "\033[33m%s\033[0m\n" "$*"; }
blue()   { printf "\033[34m%s\033[0m\n" "$*"; }

DC=""

ensure_docker() {
  if ! command -v docker >/dev/null 2>&1; then
    red "❌ 未检测到 docker。请在宝塔面板「Docker 管理器」中先安装 Docker。"
    exit 1
  fi
  if docker compose version >/dev/null 2>&1; then
    DC="docker compose"
  elif command -v docker-compose >/dev/null 2>&1; then
    DC="docker-compose"
  else
    red "❌ 未检测到 Docker Compose。请安装 docker compose v2 (推荐)。"
    red "   宝塔: Docker 管理器 → 设置 → 启用 docker-compose"
    exit 1
  fi
}

ensure_repo() {
  if ! command -v git >/dev/null 2>&1; then
    red "❌ 未检测到 git。请先安装：yum -y install git  或  apt-get -y install git"
    exit 1
  fi
  mkdir -p "$APP_DIR"
  cd "$APP_DIR"

  if [ -d ".git" ]; then
    blue "✅ 已存在仓库 $APP_DIR"
    return 0
  fi

  yellow "📦 拉取仓库到 $APP_DIR ..."

  # 兼容三种场景:
  #   1) 目录完全空            → 直接 clone
  #   2) 目录里只有用户预先 curl 的 deploy.sh
  #   3) 目录里已有 .env / data 等用户数据
  # 统一策略：先 clone 到临时目录，再把内容平移过来，
  #          已存在的 .env / data 会被保留，不会被覆盖。
  local tmpdir
  tmpdir="$(mktemp -d -t tix-clone.XXXXXX)"
  if ! git clone --depth=1 "$REPO" "$tmpdir/repo"; then
    red "❌ git clone 失败，请检查网络或 GitHub 访问"
    rm -rf "$tmpdir"
    exit 1
  fi

  shopt -s dotglob nullglob
  local src name
  for src in "$tmpdir/repo/"*; do
    name="$(basename "$src")"
    case "$name" in
      .env|.env.*)
        # 保留用户已有的 .env
        if [ -e "$APP_DIR/$name" ]; then
          continue
        fi
        ;;
      data)
        # 保留用户已有的 data 目录（SQLite 数据）
        if [ -e "$APP_DIR/data" ]; then
          continue
        fi
        ;;
    esac
    if [ -e "$APP_DIR/$name" ]; then
      rm -rf "$APP_DIR/$name"
    fi
    mv "$src" "$APP_DIR/$name"
  done
  shopt -u dotglob nullglob

  rm -rf "$tmpdir"
  green "✅ 仓库已就绪"
}

ensure_env() {
  cd "$APP_DIR"
  if [ -f .env ]; then
    return 0
  fi
  yellow "📝 生成默认 .env ..."
  local pwd_default
  pwd_default="$(LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c 18 || true)"
  pwd_default="${pwd_default:-changeme}"
  cat > .env <<EOF
TIX_PORT=${PORT}
TIX_AUTH_PASSWORD=${pwd_default}
TIX_LOG=INFO
TZ=Asia/Shanghai
EOF
  chmod 600 .env
  green "✅ 已生成随机密码: ${pwd_default}"
  green "   (保存在 ${APP_DIR}/.env)"
}

show_access() {
  cd "$APP_DIR"
  local port pass ip
  port="$(grep -E '^TIX_PORT=' .env | cut -d= -f2- || echo "$PORT")"
  pass="$(grep -E '^TIX_AUTH_PASSWORD=' .env | cut -d= -f2- || echo '<see .env>')"
  ip="$(curl -s -m 3 https://ifconfig.me 2>/dev/null \
        || curl -s -m 3 https://api.ipify.org 2>/dev/null \
        || hostname -I 2>/dev/null | awk '{print $1}')"
  ip="${ip:-<服务器IP>}"
  echo
  green "═══════════════════════════════════════════════════════"
  green " ✅ Tix 部署完成"
  green "  📍 访问地址 : http://${ip}:${port}/"
  green "  👤 用户名   : admin"
  green "  🔑 密码     : ${pass}"
  green "  📁 部署目录 : ${APP_DIR}"
  green "  💾 数据目录 : ${APP_DIR}/data  (含 SQLite tix.db)"
  green "═══════════════════════════════════════════════════════"
  echo
  echo "常用命令:"
  echo "  bash $APP_DIR/deploy.sh update      # 升级到最新版"
  echo "  bash $APP_DIR/deploy.sh logs        # 跟随日志"
  echo "  bash $APP_DIR/deploy.sh password X  # 修改密码"
  echo "  bash $APP_DIR/deploy.sh status      # 查看状态"
  echo
}

cmd_install() {
  ensure_docker
  ensure_repo
  ensure_env
  cd "$APP_DIR"
  yellow "🔨 构建镜像并启动 ..."
  $DC up -d --build
  show_access
}

cmd_update() {
  ensure_docker
  if [ ! -d "$APP_DIR/.git" ]; then
    yellow "🔁 当前目录还不是 git 仓库，自动初始化 ..."
    ensure_repo
  fi
  cd "$APP_DIR"
  yellow "🔄 拉取最新代码 ..."
  git fetch --all --prune
  git reset --hard origin/main
  ensure_env
  yellow "🔨 重新构建镜像并重启 ..."
  $DC up -d --build
  green "✅ 升级完成"
  $DC ps
}

cmd_restart() { ensure_docker; cd "$APP_DIR"; $DC restart; }
cmd_start()   { ensure_docker; cd "$APP_DIR"; $DC up -d; }
cmd_stop()    { ensure_docker; cd "$APP_DIR"; $DC stop; }
cmd_logs()    { ensure_docker; cd "$APP_DIR"; $DC logs -f --tail=200; }
cmd_status()  { ensure_docker; cd "$APP_DIR"; $DC ps; }
cmd_shell()   { ensure_docker; docker exec -it "$CONTAINER" sh; }

cmd_password() {
  ensure_docker
  cd "$APP_DIR"
  local newpwd="${1:-}"
  if [ -z "$newpwd" ]; then
    red "用法: bash deploy.sh password 新密码"
    exit 1
  fi
  if grep -q '^TIX_AUTH_PASSWORD=' .env 2>/dev/null; then
    sed -i.bak "s|^TIX_AUTH_PASSWORD=.*|TIX_AUTH_PASSWORD=${newpwd}|" .env
    rm -f .env.bak
  else
    echo "TIX_AUTH_PASSWORD=${newpwd}" >> .env
  fi
  chmod 600 .env
  green "✅ 已更新 .env 中的密码，正在重启容器使其生效 ..."
  $DC up -d
  green "✅ 完成。新密码: ${newpwd}"
}

cmd_port() {
  ensure_docker
  cd "$APP_DIR"
  local newport="${1:-}"
  if [ -z "$newport" ]; then
    red "用法: bash deploy.sh port 新端口"
    exit 1
  fi
  if grep -q '^TIX_PORT=' .env 2>/dev/null; then
    sed -i.bak "s|^TIX_PORT=.*|TIX_PORT=${newport}|" .env
    rm -f .env.bak
  else
    echo "TIX_PORT=${newport}" >> .env
  fi
  green "✅ 已更新端口为 ${newport}，正在重启容器 ..."
  $DC up -d
  green "✅ 请确认宝塔安全组 / 防火墙已放行 ${newport}"
}

cmd_uninstall() {
  ensure_docker
  cd "$APP_DIR"
  yellow "⚠️  将停止容器并删除镜像，data 目录保留"
  $DC down || true
  docker rmi tix:latest 2>/dev/null || true
  green "✅ 已卸载。如需彻底删除，请手动 rm -rf $APP_DIR"
}

usage() {
  cat <<EOF
Tix · 一键部署脚本

用法:
  bash deploy.sh install            首次部署
  bash deploy.sh update             升级到最新版本
  bash deploy.sh restart            重启
  bash deploy.sh start              启动
  bash deploy.sh stop               停止
  bash deploy.sh logs               跟随日志
  bash deploy.sh status             状态
  bash deploy.sh shell              进入容器
  bash deploy.sh password 新密码    修改 admin 密码
  bash deploy.sh port    新端口     修改对外端口
  bash deploy.sh uninstall          卸载（保留 data）

环境变量（可选）:
  APP_DIR=$APP_DIR
  PORT=$PORT
  REPO=$REPO
EOF
}

case "${1:-}" in
  install)   cmd_install ;;
  update)    cmd_update ;;
  restart)   cmd_restart ;;
  start)     cmd_start ;;
  stop)      cmd_stop ;;
  logs)      cmd_logs ;;
  status)    cmd_status ;;
  shell)     cmd_shell ;;
  password)  shift; cmd_password "${1:-}" ;;
  port)      shift; cmd_port "${1:-}" ;;
  uninstall) cmd_uninstall ;;
  ""|-h|--help|help) usage ;;
  *) red "未知命令: $1"; usage; exit 1 ;;
esac
