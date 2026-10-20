# -*- coding: utf-8 -*-
"""通用小工具：提取链接改写、响应解析、主机名校验等。"""

from __future__ import annotations

import json
import re
from typing import Any, List, Optional, Tuple, Union
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse


# ---------------------------------------------------------------------------
# 提取链接
# ---------------------------------------------------------------------------

def build_api_url(api_url: str, count: int, num_param: str = "num") -> str:
    """把提取链接里的提取个数改成 count。"""
    parts = urlparse(api_url)
    query = parse_qsl(parts.query, keep_blank_values=True)
    found = False
    new_query = []
    for k, v in query:
        if k == num_param:
            new_query.append((k, str(count)))
            found = True
        else:
            new_query.append((k, v))
    if not found:
        new_query.append((num_param, str(count)))
    return urlunparse(parts._replace(query=urlencode(new_query)))


def peek_url_num(api_url: str, num_param: str = "num") -> int:
    """读取提取链接里原本的 num 值，默认 1。"""
    query = parse_qsl(urlparse(api_url).query, keep_blank_values=True)
    for k, v in query:
        if k == num_param:
            try:
                n = int(v)
                return n if n > 0 else 1
            except ValueError:
                return 1
    return 1


# ---------------------------------------------------------------------------
# 主机名校验
# ---------------------------------------------------------------------------

_HOST_RE = re.compile(
    r"^(?:"
    r"(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)"  # IPv4
    r"|(?:[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,}"  # 域名
    r"|[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?"  # 单段主机名
    r")$"
)


def looks_like_host(host: str) -> bool:
    """粗校验主机名/IP，挡住 '{\"code' 这类残片。"""
    if not host or len(host) > 253:
        return False
    if any(ch in host for ch in '{}[]"\'\\ \t\n\r,;<>'):
        return False
    return bool(_HOST_RE.match(host))


# ---------------------------------------------------------------------------
# 响应解析
# ---------------------------------------------------------------------------

def peek_api_error(text: str) -> Optional[Tuple[Any, str]]:
    """若是业务错误 JSON，返回 (code, msg)。"""
    if not text or text[0] not in "{[":
        return None
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None
    if "code" not in obj:
        return None
    code = obj.get("code")
    if code in (0, 200, "0", "200", "success", "ok", "OK"):
        return None
    msg = obj.get("msg") or obj.get("message") or obj.get("error") or ""
    return code, str(msg)


def extract_from_json(obj: Any) -> List[Any]:
    """从各种 JSON 结构里抠出代理列表。命中一层即返回。"""
    if obj is None:
        return []
    if isinstance(obj, list):
        return list(obj)
    if isinstance(obj, str):
        return [obj] if obj.strip() else []
    if not isinstance(obj, dict):
        return []

    for key in (
        "proxy_list", "proxyList", "data", "list", "proxies",
        "result", "ip_list", "ipList", "items", "rows",
    ):
        if key not in obj:
            continue
        nested = obj[key]
        if isinstance(nested, list):
            return list(nested)
        if isinstance(nested, dict):
            got = extract_from_json(nested)
            if got:
                return got
        elif isinstance(nested, str) and nested.strip():
            return [nested]

    if any(k in obj for k in ("ip", "host", "proxy", "port")):
        return [obj]

    for v in obj.values():
        if isinstance(v, (list, dict)):
            got = extract_from_json(v)
            if got:
                return got
    return []


def parse_api_response(
    body: Union[str, bytes, dict, list],
    fmt: str = "auto",
    text_sep: str = "auto",
) -> List[Any]:
    """
    把厂商返回解析成代理原始条目列表。
    fmt: auto | json | text

    合法 JSON（含错误包）只走 JSON 解析，绝不按逗号拆文本。
    """
    if isinstance(body, (dict, list)):
        return extract_from_json(body)

    text = body.decode("utf-8", errors="replace") if isinstance(body, bytes) else str(body)
    text = text.strip()
    if not text:
        return []

    if fmt in ("auto", "json"):
        try:
            obj = json.loads(text)
        except json.JSONDecodeError:
            obj = None
        if obj is not None:
            return extract_from_json(obj)
        if fmt == "json":
            return []

    if fmt not in ("auto", "text"):
        return []

    if text_sep == "\\n":
        text_sep = "\n"
    if text_sep != "auto":
        chunks = [c.strip() for c in text.split(text_sep)]
    else:
        if "\n" in text or "\r" in text:
            chunks = [c.strip() for c in re.split(r"[\r\n]+", text)]
        elif "," in text:
            chunks = [c.strip() for c in text.split(",")]
        elif ";" in text:
            chunks = [c.strip() for c in text.split(";")]
        elif "|" in text:
            chunks = [c.strip() for c in text.split("|")]
        else:
            chunks = [text.strip()]

    out = []
    for c in chunks:
        if not c:
            continue
        if c[0] in "{[\"'" or ("code" in c.lower() and "msg" in c.lower()):
            continue
        out.append(c)
    return out


def safe_json(obj: Any) -> Any:
    """给 Flask json 响应用的小工具。"""
    return json.loads(json.dumps(obj, ensure_ascii=False, default=str))
