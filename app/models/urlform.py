"""手写 application/x-www-form-urlencoded 解析器

为什么不用 FastAPI 的 Form()：
Form() 依赖 `python-multipart`，而本项目刻意保持零额外运行时依赖
（只用 fastapi / uvicorn / pydantic + 标准库）。
受理渠道本来就包括「静态托管的隐私门户表单」这种直接 POST 表单的路径，
所以这里需要一个能独立工作、且行为可单测的解析器。

刻意实现的细节：
- `+` 解码为空格（这是 urlencoded 与 percent-encoding 唯一的历史差异，
  漏掉会导致 "John+Doe" 变成 "John+Doe" 而非 "John Doe"）
- 重复键收集成列表（`categories=A&categories=B`）
- 空值保留（`x=` → `{"x": ""}`），因为「明确置空」和「未提供」语义不同
- 容错解码（用 errors="replace" 而非抛异常，畸形输入不该让受理流程 500）
"""
from __future__ import annotations

from urllib.parse import unquote_plus


def parse_urlencoded(body: str | bytes) -> dict:
    """
    解析 urlencoded 请求体 → dict[str, str | list[str]]

    >>> parse_urlencoded("a=1&b=hello+world&b=2&c=")
    {'a': '1', 'b': ['hello world', '2'], 'c': ''}
    """
    if body is None:
        return {}
    if isinstance(body, (bytes, bytearray)):
        body = body.decode("utf-8", errors="replace")
    if not isinstance(body, str):
        raise TypeError(f"expected str or bytes, got {type(body).__name__}")

    result: dict = {}
    if not body.strip():
        return result

    for pair in body.split("&"):
        if not pair:
            continue
        key, sep, value = pair.partition("=")
        key = unquote_plus(key, errors="replace")
        # 畸形输入（没有 `=`）把整段当作 key，值取空 —— 不静默丢弃
        value = unquote_plus(value, errors="replace") if sep else ""
        if key in result:
            existing = result[key]
            if isinstance(existing, list):
                existing.append(value)
            else:
                result[key] = [existing, value]
        else:
            result[key] = value
    return result


def first(values, default=None):
    """从单值/多值结果里取第一个 —— 兼容 parse_urlencoded 的重复键行为"""
    if values is None:
        return default
    if isinstance(values, list):
        return values[0] if values else default
    return values


def as_list(values) -> list[str]:
    """把单值/多值统一成列表，逗号分隔也展开（兼容 `a=1,2` 与 `a=1&a=2`）"""
    if values is None:
        return []
    items = values if isinstance(values, list) else [values]
    out: list[str] = []
    for item in items:
        for part in str(item).split(","):
            part = part.strip()
            if part:
                out.append(part)
    return out
