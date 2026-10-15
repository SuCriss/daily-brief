"""图片字段归一化。

上游各家图片格式不一致，实测有两种：
  财联社 : ["https://image.cls.cn/xxx.png"]                     —— 字符串列表
  新浪   : [{"u":"https://n.sinaimg.cn/xxx.png","w":400,"h":300,"t":""}]  —— 字典列表

另外新浪会给没有配图的文章塞一张 100x100 的默认占位图
（wx_poster_finance1.png），必须过滤掉，否则页面全是灰块。

本模块把两种格式统一成「URL 字符串列表」，并完成：
  1. 字典 → 取 u / url / img 字段
  2. 过滤占位图、空白、非法 scheme
  3. http → https 升级（避免 HTTPS 部署时的混合内容拦截）
  4. 去重、限量
"""
from __future__ import annotations

from typing import Any, Iterable
from urllib.parse import urlparse

# 单条快讯最多展示的图片数
MAX_IMAGES = 6

# 新浪默认占位图特征
PLACEHOLDER_MARKERS = (
    "wx_poster_finance",
    "wx_poster",
    "default_image",
    "placeholder",
    # 财联社分享卡片占位图（img.cls.cn/share/roll.png、share/vip.png）
    "share/roll",
    "share/vip",
)

# 允许 http→https 升级的图床（这些域名实测支持 https）
HTTPS_UPGRADE_HOSTS = (
    "n.sinaimg.cn",
    "image.cls.cn",
    "img.cls.cn",
)

# 允许走代理的图床白名单（用于兜底与混合内容场景）
PROXY_ALLOWED_HOSTS = (
    "n.sinaimg.cn",
    "image.cls.cn",
    "img.cls.cn",
    "sinaimg.cn",
    "cls.cn",
)

# 占位图尺寸阈值：宽高都不超过该值视为占位
PLACEHOLDER_MAX_DIM = 120


def _extract_url(item: Any) -> str:
    """从单个条目里取出 URL。兼容字符串与各家字典格式。"""
    if isinstance(item, str):
        return item.strip()
    if isinstance(item, dict):
        for key in ("u", "url", "img", "image", "src", "cover", "thumb"):
            v = item.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()
    return ""


def _is_placeholder(item: Any, url: str) -> bool:
    """判断是否为默认占位图。"""
    low = url.lower()
    if any(m in low for m in PLACEHOLDER_MARKERS):
        return True
    if isinstance(item, dict):
        w, h = item.get("w"), item.get("h")
        try:
            if w is not None and h is not None:
                if max(int(float(w)), int(float(h))) <= PLACEHOLDER_MAX_DIM:
                    return True
        except (TypeError, ValueError):
            pass
    return False


def _upgrade_scheme(url: str) -> str:
    """把已知支持 https 的图床 http 链接升级为 https。"""
    if url.startswith("http://"):
        try:
            host = urlparse(url).netloc.lower()
        except ValueError:
            return url
        if any(host == h or host.endswith("." + h) for h in HTTPS_UPGRADE_HOSTS):
            return "https://" + url[len("http://"):]
    return url


def _is_valid(url: str) -> bool:
    """只接受 http(s) 或 data: 图片链接。"""
    if not url:
        return False
    if url.startswith("data:image/"):
        return True
    if not url.startswith(("http://", "https://")):
        return False
    try:
        host = urlparse(url).netloc
    except ValueError:
        return False
    return bool(host)


def normalize_images(raw: Any) -> list[str]:
    """把任意上游图片字段归一化为 URL 字符串列表。

    raw 可以是：None / JSON 字符串 / list / 单元素 dict / 单字符串。
    """
    if raw is None:
        return []

    items: Iterable[Any]
    if isinstance(raw, str):
        s = raw.strip()
        if not s or s == "[]":
            return []
        # 尝试按 JSON 解析，失败则当单个 URL
        if s[0] in "[{":
            import json

            try:
                parsed = json.loads(s)
            except (ValueError, TypeError):
                return []
            return normalize_images(parsed)
        items = [s]
    elif isinstance(raw, list):
        items = raw
    elif isinstance(raw, dict):
        items = [raw]
    else:
        return []

    out: list[str] = []
    seen: set[str] = set()
    for it in items:
        url = _extract_url(it)
        if not _is_valid(url):
            continue
        if _is_placeholder(it, url):
            continue
        url = _upgrade_scheme(url)
        if url in seen:
            continue
        seen.add(url)
        out.append(url)
        if len(out) >= MAX_IMAGES:
            break
    return out


def proxy_url(url: str) -> str:
    """生成走本站图片代理的链接（兜底用）。"""
    from urllib.parse import quote

    return "/img?u=" + quote(url, safe="")


def proxy_allowed(url: str) -> bool:
    """判断 URL 是否允许走本站代理（白名单，防 SSRF）。"""
    if not url.startswith(("http://", "https://")):
        return False
    try:
        host = urlparse(url).netloc.lower().split("@")[-1].split(":")[0]
    except ValueError:
        return False
    return any(host == h or host.endswith("." + h) for h in PROXY_ALLOWED_HOSTS)
