"""FastAPI 应用入口 —— DSAR Management Platform"""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .api.routes import router
from .models.seed import seed_all

ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = ROOT / "static"

app = FastAPI(
    title="DSAR Management Platform",
    version="1.0.0",
    description=(
        "GDPR Art. 12-22 数据主体请求（DSAR）全生命周期管理："
        "身份核验 → 法定时限倒计时 → 跨系统检索编排 → 脱敏响应 → 留存销毁。\n\n"
        "**数据说明**：默认加载的全部是**合成演示数据** —— 姓名、邮箱、证件号、卡号、"
        "企业系统名均为虚构构造值。本项目未对接任何真实的 HR / CRM / 客服 / 分析系统，"
        "也不包含任何真实用户的个人数据或真实合规记录。\n\n"
        "GDPR / CCPA 条款引用为公开法规文本，用于说明流程设计依据。"
    ),
)
app.include_router(router)


@app.get("/", include_in_schema=False)
def dashboard():
    if (STATIC_DIR / "index.html").exists():
        return FileResponse(STATIC_DIR / "index.html")
    return {"detail": "static/index.html not found", "docs": "/docs"}


if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


def run(host: str = "127.0.0.1", port: int = 5054, reload: bool = False):
    import uvicorn
    if not os.environ.get("SKIP_SEED"):
        seed_all()
    uvicorn.run("app.main:app", host=host, port=port, reload=reload)
