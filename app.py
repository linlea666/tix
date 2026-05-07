"""
Tix · 多币种黑天鹅监控系统 — FastAPI 入口。

启动:
    python app.py
或:
    TIX_AUTH_PASSWORD=xxx uvicorn app:app --host 0.0.0.0 --port 8000

环境变量:
    TIX_CONFIG          配置文件路径（默认 config.json）
    TIX_STATE           告警历史文件路径（默认 state.json）
    TIX_AUTH_PASSWORD   覆盖 Web 后台密码（用户名固定 admin，可在 config.json 改）
    TIX_LOG             日志级别（默认 INFO）
    TIX_HOST / TIX_PORT 监听地址端口
"""
from __future__ import annotations

import logging
import os
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
)
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

import config
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
    config.load()
    state.store.restore()
    await feed.manager.start()
    log.info("tix started: %d coins", len(config.get().coins))
    try:
        yield
    finally:
        await feed.manager.stop()
        state.store.persist()
        log.info("tix stopped")


app = FastAPI(title="Tix · Black Swan Monitor", lifespan=lifespan)


# -------------------------------- routes ---------------------------------


@app.get("/", response_class=HTMLResponse)
def index(request: Request, _user: str = Depends(auth)) -> HTMLResponse:
    cfg = config.get()
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "cfg": cfg,
            "snapshot": state.store.snapshot(),
            "now": time.time(),
        },
    )


def _parse_floats(raw: str) -> list[float]:
    return [
        float(x)
        for x in raw.replace(" ", "").split(",")
        if x.strip()
    ]


@app.post("/save")
async def save(
    request: Request, _user: str = Depends(auth)
) -> RedirectResponse | JSONResponse:
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

    submitted: set[str] = set()
    for key in form.keys():
        if key.startswith("coin__") and key.endswith("__symbol"):
            v = (form.get(key) or "").strip().lower()
            if v:
                submitted.add(v)

    coins: dict[str, config.CoinConfig] = {}
    for sym in submitted:
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
            )
        except (ValueError, ValidationError) as e:
            return JSONResponse({"error": f"{sym}: {e}"}, status_code=400)

    add_sym = (form.get("add_symbol") or "").strip().lower()
    if add_sym and add_sym not in coins:
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
    return RedirectResponse("/", status_code=302)


@app.post("/coin/{symbol}/delete")
async def delete_coin(
    symbol: str, _user: str = Depends(auth)
) -> RedirectResponse:
    sym = symbol.lower().strip()
    cfg = config.get()
    if sym in cfg.coins:
        cfg.coins.pop(sym)
        config.update(cfg)
        state.store.drop(sym)
        await feed.manager.refresh()
    return RedirectResponse("/", status_code=302)


@app.get("/test")
def test_alert(_user: str = Depends(auth)) -> dict:
    delivered = notifier.dispatch(
        "🚨 Tix 测试通知",
        f"如果你收到这条消息，说明系统运行正常 · {time.strftime('%F %T')}",
    )
    return {"ok": True, "delivered": delivered}


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


@app.get("/healthz", response_class=PlainTextResponse)
def healthz() -> PlainTextResponse:
    snap = state.store.snapshot()
    coins = snap["coins"]
    if not coins:
        return PlainTextResponse("OK (no coins configured)\n")
    bad = [s for s, c in coins.items() if not c["connected"]]
    if bad:
        return PlainTextResponse(
            f"DEGRADED: not connected: {','.join(bad)}\n",
            status_code=503,
        )
    stale = [
        s for s, c in coins.items()
        if c["last_update"] and time.time() - c["last_update"] > 60
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
