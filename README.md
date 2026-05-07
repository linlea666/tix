# Tix · 多币种黑天鹅监控系统

基于 **FastAPI + SQLite + Binance Combined WebSocket** 的 7×24 价格监控守护进程，
带 Web 后台、可视化配置、阈值穿越/暴跌/崩盘三种告警模型，多通道推送（Pushover / Telegram / Webhook），
支持 **Docker 一键部署** 与 **一键升级**。

仓库地址：<https://github.com/linlea666/tix>

---

## 🚀 一键部署（推荐：Docker + 宝塔）

> 适配宝塔面板，也支持任何已经安装了 Docker / Docker Compose 的 Linux 服务器。

### 1. 首次部署

SSH 登录服务器后执行：

```bash
mkdir -p /www/wwwroot/tix && cd /www/wwwroot/tix
curl -fsSL https://raw.githubusercontent.com/linlea666/tix/main/deploy.sh -o deploy.sh
chmod +x deploy.sh
bash deploy.sh install
```

脚本会自动：

1. 检查 Docker / Compose
2. 克隆仓库到 `/www/wwwroot/tix`
3. 生成随机强密码到 `/www/wwwroot/tix/.env`
4. 构建镜像并启动容器
5. 打印**访问地址 / 用户名 / 密码**

完成后，宝塔的「Docker 容器」面板里会出现 `tix` 容器（参考你截图中 `polyhermes` 的样式）。

### 2. 升级到最新版本

```bash
bash /www/wwwroot/tix/deploy.sh update
```

会自动 `git pull` + 重建镜像 + 平滑重启，**不影响 `data/` 目录里的 SQLite 数据**。

### 3. 其他常用命令

```bash
bash deploy.sh status            # 查看运行状态
bash deploy.sh logs              # 跟随日志（Ctrl-C 退出）
bash deploy.sh restart           # 重启
bash deploy.sh stop / start      # 停止 / 启动
bash deploy.sh password 新密码    # 修改 admin 密码
bash deploy.sh port    新端口     # 修改对外端口（记得放行宝塔安全组）
bash deploy.sh shell             # 进入容器
bash deploy.sh uninstall         # 卸载（保留 data 目录）
```

### 4. 宝塔界面操作

- 容器名：`tix`
- 默认端口映射：`宿主机 8688 → 容器 8000`
- 镜像名：`tix:latest`
- 数据卷：`/www/wwwroot/tix/data` → `/app/data`（**SQLite 数据落在这里，请勿删除**）
- 健康检查：`docker ps` 状态会显示 `healthy`

> 如果端口被占用或想换端口，运行 `bash deploy.sh port 9999` 即可。

---

## 🧰 功能特性

| 模块 | 说明 |
| --- | --- |
| 数据源 | Binance Combined WebSocket（**单连接复用全部币种**） |
| 异步模型 | asyncio 单事件循环 + 指数退避重连 (1s → 60s) + 心跳超时 |
| 持久层 | **SQLite + WAL**：`coins / settings / alerts` 三表 |
| 告警模型 | ① 阈值跌破 ② 阈值突破 ③ 瞬时暴跌 ④ 持续崩盘 |
| 抗抖动 | **阈值迟滞带 hysteresis_pct**（默认 0.5%） + 独立 cooldown |
| 多通道 | Pushover (priority=2) / Telegram / Webhook，**可单通道测试** |
| Web UI | 实时仪表板 · 增删币 · 启用/暂停 · 修改密码 · 清空告警 · 单通道测试 |
| 安全 | BasicAuth + 默认随机强密码 + 敏感字段 API 自动遮罩 |
| 可观测 | `/healthz` · `/api/snapshot` · `/api/config` · `/api/alerts` · 标准 logging |
| 容器 | 多阶段缓存 / Healthcheck / 日志轮转 / 数据卷持久化 |

---

## 🖥️ 本地开发

```bash
git clone https://github.com/linlea666/tix.git && cd tix
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
TIX_AUTH_PASSWORD=admin python app.py
# 浏览器访问 http://localhost:8000/  → admin / admin
```

数据会写到 `./data/tix.db`。

---

## 🌍 环境变量

| 变量 | 说明 | 默认 |
| --- | --- | --- |
| `TIX_HOST` | 监听地址 | `0.0.0.0` |
| `TIX_PORT` | 容器内监听端口（不要随便改）| `8000` |
| `TIX_DB` | SQLite 文件路径 | `data/tix.db` |
| `TIX_AUTH_PASSWORD` | 覆盖 admin 密码（运行时优先于 DB）| `changeme` |
| `TIX_LOG` | 日志级别 | `INFO` |
| `TZ` | 时区 | `Asia/Shanghai` |

`docker-compose.yml` 通过 `.env` 注入 `TIX_PORT / TIX_AUTH_PASSWORD` 等。

---

## 🔌 API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/` | Web 仪表板 |
| POST | `/save` | 保存全部配置（表单） |
| POST | `/coin/{symbol}/delete` | 删除币种 |
| POST | `/coin/{symbol}/toggle` | 启用/暂停币种 |
| POST | `/password` | 修改 admin 密码 |
| POST | `/alerts/clear` | 清空告警历史 |
| GET | `/test?channel=pushover|telegram|webhook` | 触发测试通知 |
| GET | `/api/snapshot` | 实时价 + 告警 + 连接状态 JSON |
| GET | `/api/alerts?limit=&symbol=` | 历史告警分页查询 |
| GET | `/api/config` | 配置（敏感字段已遮罩） |
| GET | `/healthz` | 健康检查（200 OK / 503 DEGRADED） |

---

## 🗄️ 数据 & 备份

`data/tix.db` 是 SQLite，包含：

- `coins`：每币的阈值/窗口/cooldown/迟滞带/启用状态
- `settings`：通知通道配置 + 后台账号
- `alerts`：告警历史（自动按容量保留）

备份只需复制整个 `data/` 目录：

```bash
cp -a /www/wwwroot/tix/data /backup/tix-data-$(date +%F)
```

---

## 🧪 阈值迟滞带（hysteresis）说明

为防止价格在阈值附近来回震荡导致重复推送：

- **跌破**：触发后必须反弹到 `level × (1 + hyst%)` 才解除
- **突破**：触发后必须回落到 `level × (1 − hyst%)` 才解除
- **暴跌 / 崩盘**：跌幅恢复到 `drop_pct + |drop_pct| × hyst%` 才解除

默认 `hyst = 0.5%`，可在每个币种的卡片中独立调整。

---

## 🛡️ 安全建议

1. **修改默认密码**：首次部署后，命令行 `bash deploy.sh password X` 或 Web 后台修改。
2. **HTTPS**：公网部署建议在前面挂 Nginx + Let's Encrypt，仅暴露 443。宝塔可一键反代。
3. **`.env`** 自动 `chmod 600`，请勿提交到仓库。
4. **`data/tix.db`** 包含 token，注意目录权限。

---

## 📂 项目结构

```
tix/
├── app.py            # FastAPI 入口、路由、生命周期
├── config.py         # Pydantic schema + SQLite 持久化
├── db.py             # SQLite 连接池 / Schema / 事务
├── state.py          # 运行时状态 + 告警表
├── monitor.py        # 价格状态机
├── feed.py           # Binance Combined WS（asyncio 单连接）
├── notifier.py       # Pushover / Telegram / Webhook
├── templates/
│   └── index.html    # 仪表板 + 配置 + 改密码
├── Dockerfile
├── docker-compose.yml
├── deploy.sh         # 一键部署 / 升级 / 改密码
├── requirements.txt
└── README.md
```
