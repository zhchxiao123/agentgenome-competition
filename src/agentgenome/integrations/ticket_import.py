"""工单链接导入。

**本期至少支持一种主流平台**——选 GitHub issue,因为公开仓库的 issue 读不需要认证,能在
没有任何凭证的部署里也跑通最小闭环。私有工单系统(Jira/禅道/…)接的是同一个 `TicketFetcher`
协议,换一个实现、注册进 `FETCHERS` 就行,不必改调用方。

## 请求函数可替换

`http_get` 参数是这一层的次测试缝:真实实现打网络,测试注入一个返回预录内容的假函数。
不做成一整个 Protocol 类是因为这里只有一个方法,一个可调用对象就够表达这条缝了。
"""

from __future__ import annotations

import json
import re
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass

#: 请求超时。导入是用户等在界面上的同步操作,不能无限等。
TIMEOUT_S = 10

_GITHUB_ISSUE_URL = re.compile(
    r"^https://github\.com/(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+)/issues/(?P<number>\d+)/?$"
)

HttpGet = Callable[[str], str]


@dataclass(frozen=True)
class TicketContent:
    title: str
    body: str
    source: str


class UnsupportedTicketUrl(ValueError):
    """这个链接不属于任何已注册的工单平台。"""


def _default_get(url: str) -> str:
    request = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:  # noqa: S310
        body: bytes = response.read()
        return body.decode("utf-8")


def import_ticket(url: str, http_get: HttpGet | None = None) -> TicketContent:
    """解析一个工单链接,抓取标题与正文。"""
    get = http_get or _default_get
    match = _GITHUB_ISSUE_URL.match(url.strip())
    if not match:
        raise UnsupportedTicketUrl(
            f"暂不支持这个链接: {url}(目前只支持 GitHub issue,"
            "形如 https://github.com/<owner>/<repo>/issues/<number>)"
        )
    api_url = (
        f"https://api.github.com/repos/{match['owner']}/{match['repo']}/issues/{match['number']}"
    )
    payload = json.loads(get(api_url))
    return TicketContent(
        title=str(payload.get("title") or ""),
        body=str(payload.get("body") or ""),
        source=url,
    )


__all__ = ["TIMEOUT_S", "HttpGet", "TicketContent", "UnsupportedTicketUrl", "import_ticket"]
