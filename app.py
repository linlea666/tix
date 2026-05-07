"""
Tix · 多币种黑天鹅监控系统 — FastAPI 入口。

启动:
    python app.py
或:
    TIX_AUTH_PASSWORD=xxx uvicorn app:app --host 0.0.0.0 --port 8000

环境变量:
    TIX_DB              SQLite 文件路径（默认 data/tix.db）
    TIX_AUTH_PASSWORD   覆盖 Web 后台密码（用户名固定 admin，可在 UI 改）
    TIX_LOG             日志级别（默认 INFO）
    TIX_HOST / TIX_PORT 监听地址 / 端口
"""
from __future__ import annotations

import logging
import os
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional
from urllib.parse import quote, unquote

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

import config
import db
import feed
import notifier
import state

logging.basicConfig(
    level=os.environ.get("TIX_LOG", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("tix")

BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
security = HTTPBasic(realm="tix")


def auth(creds: HTTPBasicCredentials = Depends(security)) -> str:
    cfg = config.get().auth
    ok_user = secrets.compare_digest(creds.username, cfg.username)
    ok_pass = secrets.compare_digest(creds.password, cfg.password)
    if not (ok_user and ok_pass):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid credentials",
            headers={"WWW-Authenticate": 'Basic realm="tix"'},
        )
    return creds.username


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init()
    config.load()
    await feed.manager.start()
    log.info("tix started: %d coins", len(config.get().coins))
    try:
        yield
    finally:
        await feed.manager.stop()
        db.close()
        log.info("tix stopped")


app = FastAPI(title="Tix · Black Swan Monitor", lifespan=lifespan)


# ------------------------------ helpers ------------------------------


def _parse_floats(raw: str) -> list[float]:
    return [
        float(x)
        for x in (raw or "").replace(" ", "").split(",")
        if x.strip()
    ]


def _flash(request: Request, msg: str, level: str = "ok") -> RedirectResponse:
    """通过 cookie 让下一次刷新页面顶部显示一条消息。
    Set-Cookie 头只能 latin-1，所以 emoji/中文先 URL 编码。"""
    resp = RedirectResponse("/", status_code=302)
    resp.set_cookie(
        "tix_flash",
        quote(f"{level}|{msg}", safe=""),
        max_age=5,
    )
    return resp


# ------------------------------ pages ------------------------------


@app.get("/", response_class=HTMLResponse)
def index(request: Request, _user: str = Depends(auth)) -> HTMLResponse:
    cfg = config.get()
    snap = state.store.snapshot()

    flash = None
    raw_flash = request.cookies.get("tix_flash")
    if raw_flash:
        try:
            decoded = unquote(raw_flash)
            if "|" in decoded:
                level, _, msg = decoded.partition("|")
                flash = {"level": level, "msg": msg}
        except (ValueError, UnicodeDecodeError):
            flash = None

    resp = templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "cfg": cfg,
            "snapshot": snap,
            "now": time.time(),
            "flash": flash,
        },
    )
    if raw_flash:
        resp.delete_cookie("tix_flash")
    return resp


# ------------------------------ config ------------------------------


@app.post("/save")
async def save(
    request: Request, _user: str = Depends(auth)
) -> Response:
    form = await request.form()
    cfg = config.get()

    cfg.pushover.enabled = bool(form.get("pushover_enabled"))
    cfg.pushover.token = (form.get("pushover_token") or "").strip()
    cfg.pushover.user = (form.get("pushover_user") or "").strip()

    cfg.telegram.enabled = bool(form.get("telegram_enabled"))
    cfg.telegram.bot_token = (form.get("telegram_bot_token") or "").strip()
    cfg.telegram.chat_id = (form.get("telegram_chat_id") or "").strip()

    cfg.webhook.enabled = bool(form.get("webhook_enabled"))
    cfg.webhook.url = (form.get("webhook_url") or "").strip()

    submitted_syms: set[str] = set()
    for key in form.keys():
        if key.startswith("coin__") and key.endswith("__symbol"):
            v = (form.get(key) or "").strip().lower()
            if v:
                submitted_syms.add(v)

    coins: dict[str, config.CoinConfig] = {}
    for sym in submitted_syms:
        try:
            coins[sym] = config.CoinConfig(
                price_below_list=_parse_floats(
                    form.get(f"coin__{sym}__below") or ""
                ),
                price_above_list=_parse_floats(
                    form.get(f"coin__{sym}__above") or ""
                ),
                flash_crash=config.CrashRule(
                    window_sec=int(form.get(f"coin__{sym}__flash_window") or 60),
                    drop_pct=float(form.get(f"coin__{sym}__flash_drop") or -2.5),
                ),
                slow_crash=config.CrashRule(
                    window_sec=int(form.get(f"coin__{sym}__slow_window") or 300),
                    drop_pct=float(form.get(f"coin__{sym}__slow_drop") or -5.0),
                ),
                cooldown=int(form.get(f"coin__{sym}__cooldown") or 300),
                hysteresis_pct=float(form.get(f"coin__{sym}__hysteresis") or 0.5),
                enabled=bool(form.get(f"coin__{sym}__enabled")),
            )
        except (ValueError, ValidationError) as e:
            return JSONResponse({"error": f"{sym}: {e}"}, status_code=400)

    add_sym = (form.get("add_symbol") or "").strip().lower()
    if add_sym:
        # 简单合法性：字母+数字，3-20 位
        if not (3 <= len(add_sym) <= 20 and add_sym.isalnum()):
            return JSONResponse(
                {"error": f"非法 symbol: {add_sym}"}, status_code=400
            )
        if add_sym not in coins:
            coins[add_sym] = config.CoinConfig()

    cfg.coins = coins
    try:
        config.update(cfg)
    except ValidationError as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    for sym in state.store.symbols():
        if sym not in coins:
            state.store.drop(sym)

    await feed.manager.refresh()
    return _flash(request, "✅ 配置已保存", "ok")


@app.post("/coin/{symbol}/delete")
async def delete_coin(
    symbol: str, request: Request, _user: str = Depends(auth)
) -> Response:
    sym = symbol.lower().strip()
    cfg = config.get()
    if sym in cfg.coins:
        cfg.coins.pop(sym)
        config.update(cfg)
        state.store.drop(sym)
        await feed.manager.refresh()
        return _flash(request, f"🗑️ 已删除 {sym.upper()}", "ok")
    return _flash(request, f"未找到币种 {sym}", "warn")


@app.post("/coin/{symbol}/toggle")
async def toggle_coin(
    symbol: str, request: Request, _user: str = Depends(auth)
) -> Response:
    sym = symbol.lower().strip()
    cfg = config.get()
    if sym in cfg.coins:
        cfg.coins[sym].enabled = not cfg.coins[sym].enabled
        config.update(cfg)
        return _flash(
            request,
            f"{'▶' if cfg.coins[sym].enabled else '⏸'} {sym.upper()} 已"
            f"{'启用' if cfg.coins[sym].enabled else '暂停'}",
            "ok",
        )
    return _flash(request, f"未找到币种 {sym}", "warn")


# ------------------------------ password ------------------------------


@app.post("/password")
async def change_password(
    request: Request, _user: str = Depends(auth)
) -> Response:
    form = await request.form()
    new1 = (form.get("password_new") or "").strip()
    new2 = (form.get("password_new2") or "").strip()
    if not new1:
        return _flash(request, "新密码不能为空", "warn")
    if new1 != new2:
        return _flash(request, "两次输入不一致", "warn")
    try:
        config.update_password(new1)
    except ValueError as e:
        return _flash(request, f"密码不合法: {e}", "warn")
    return _flash(request, "🔑 密码已更新，请用新密码重新登录", "ok")


# ------------------------------ alerts ------------------------------


@app.post("/alerts/clear")
async def clear_alerts(
    request: Request, _user: str = Depends(auth)
) -> Response:
    n = state.store.clear_alerts()
    return _flash(request, f"🧹 已清空 {n} 条告警历史", "ok")


# ------------------------------ notifications ------------------------------


@app.get("/test")
def test_alert(
    channel: Optional[str] = None, _user: str = Depends(auth)
) -> dict:
    title = "🚨 Tix 测试通知"
    msg = f"如果你收到这条消息，说明系统运行正常 · {time.strftime('%F %T')}"
    if channel:
        ok = notifier.dispatch_one(channel, title, msg)
        return {"ok": ok, "channel": channel}
    delivered = notifier.dispatch(title, msg)
    return {"ok": True, "delivered": delivered}


# ------------------------------ json api ------------------------------


@app.get("/api/snapshot")
def api_snapshot(_user: str = Depends(auth)) -> dict:
    return state.store.snapshot()


@app.get("/api/config")
def api_config(_user: str = Depends(auth)) -> dict:
    cfg = config.get().model_dump()
    cfg["pushover"]["token"] = "***" if cfg["pushover"]["token"] else ""
    cfg["telegram"]["bot_token"] = "***" if cfg["telegram"]["bot_token"] else ""
    cfg["auth"]["password"] = "***"
    return cfg


@app.get("/api/alerts")
def api_alerts(
    limit: int = 100,
    symbol: Optional[str] = None,
    _user: str = Depends(auth),
) -> dict:
    rs = state.store.alerts(limit=min(max(1, limit), 1000), symbol=symbol)
    return {"alerts": [a.to_dict() for a in rs]}


# ------------------------------ health ------------------------------


@app.get("/healthz", response_class=PlainTextResponse)
def healthz() -> PlainTextResponse:
    cfg_coins = [
        s for s, c in config.get().coins.items() if c.enabled
    ]
    if not cfg_coins:
        return PlainTextResponse("OK (no enabled coins)\n")
    snap = state.store.snapshot()["coins"]
    bad = [s for s in cfg_coins if not snap.get(s, {}).get("connected")]
    if bad:
        return PlainTextResponse(
            f"DEGRADED: not connected: {','.join(bad)}\n",
            status_code=503,
        )
    now = time.time()
    stale = [
        s for s in cfg_coins
        if snap.get(s, {}).get("last_update")
        and now - snap[s]["last_update"] > 60
    ]
    if stale:
        return PlainTextResponse(
            f"DEGRADED: stale data: {','.join(stale)}\n",
            status_code=503,
        )
    return PlainTextResponse("OK\n")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app:app",
        host=os.environ.get("TIX_HOST", "0.0.0.0"),
        port=int(os.environ.get("TIX_PORT", "8000")),
        reload=False,
        log_config=None,
    )
