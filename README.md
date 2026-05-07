# Tix · 多币种黑天鹅监控系统

基于 FastAPI + Binance Combined WebSocket + 多通道告警的 7×24 价格监控守护进程。

## 特性

- 单条 WebSocket 同时监听多币 ticker（Binance Combined Stream），不再一币一线程
- 异步事件循环 + 指数退避重连（1 → 60 秒）
- Pydantic 配置 schema + 原子写入 + 线程安全
- 阈值穿越（below/above）+ 瞬时暴跌 + 持续崩盘
- 阈值迟滞带（hysteresis）防止价格抖动反复触发
- 多通道告警：Pushover / Telegram / Webhook
- BasicAuth 保护的 Web 后台 + JSON API
- `/healthz` 端点：连接/数据陈旧检测，可对接 K8s / Docker healthcheck
- 告警历史持久化（重启后保留）
- Web UI 平滑增删币种，自动重建 WS 连接，无需重启进程

## 安装与运行

```bash
pip install -r requirements.txt

# 第一次启动会自动生成 config.json（默认密码 changeme）
TIX_AUTH_PASSWORD=your-strong-password python app.py
```

打开浏览器访问 `http://localhost:8000/`，使用 `admin / 上面设置的密码` 登录。

### Docker

```bash
docker build -t tix .
docker run -d --name tix \
  -p 8000:8000 \
  -e TIX_AUTH_PASSWORD=your-strong-password \
  -v $PWD/data:/app \
  tix
```

## 环境变量

| 变量 | 说明 | 默认 |
| --- | --- | --- |
| `TIX_HOST` | 监听地址 | `0.0.0.0` |
| `TIX_PORT` | 监听端口 | `8000` |
| `TIX_CONFIG` | 配置文件路径 | `config.json` |
| `TIX_STATE` | 告警历史文件 | `state.json` |
| `TIX_AUTH_PASSWORD` | 覆盖 Web 密码 | `changeme` |
| `TIX_LOG` | 日志级别 | `INFO` |

## API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/` | Web 仪表板 + 配置表单 |
| POST | `/save` | 保存配置（表单提交） |
| POST | `/coin/{symbol}/delete` | 删除币种 |
| GET | `/test` | 触发一次测试通知 |
| GET | `/api/snapshot` | 实时价 + 连接状态 + 告警历史 JSON |
| GET | `/api/config` | 当前配置（敏感字段已遮罩） |
| GET | `/healthz` | 健康检查（200 OK / 503 DEGRADED） |

## 阈值迟滞带（hysteresis）

`hysteresis_pct` 默认 0.5%：

- 价格跌破 80000 触发告警 → 必须反弹到 80000 × (1 + 0.5%) = 80400 才解除告警态。
- 解除前即使再次跌破也不会重复推送（cooldown 之外的二级保护）。
- 暴跌 / 崩盘类告警的解除条件：跌幅恢复到 `drop_pct + |drop_pct| × hyst%` 以上。

## 安全建议

1. 第一次启动后**立刻修改默认密码**（环境变量或 `config.json` → `auth`）。
2. 公网部署时建议在前面挂一层 Nginx + HTTPS，并把 BasicAuth 升级为更强的方案。
3. `config.json` 含 Pushover/Telegram token，注意目录权限（`chmod 600`）。

## 模块结构

```
app.py        FastAPI 入口、路由、生命周期
config.py     Pydantic schema + 原子持久化
state.py      运行时状态 + 告警历史
monitor.py    价格状态机（阈值/暴跌/崩盘 + 迟滞）
feed.py       Binance Combined WS（asyncio 单连接）
notifier.py   多通道通知
templates/    Jinja2 模板
```
