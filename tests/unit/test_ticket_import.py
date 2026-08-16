"""工单导入的次测试缝:`http_get` 可替换,不必打真网络。"""

from __future__ import annotations

import json

import pytest

from agentgenome.integrations.ticket_import import UnsupportedTicketUrl, import_ticket


def test_github_issue_url_is_parsed_and_fetched() -> None:
    calls = []

    def fake_get(url: str) -> str:
        calls.append(url)
        return json.dumps({"title": "登录页 500", "body": "复现步骤: ..."})

    result = import_ticket("https://github.com/acme/widget/issues/42", http_get=fake_get)

    assert calls == ["https://api.github.com/repos/acme/widget/issues/42"]
    assert result.title == "登录页 500"
    assert result.body == "复现步骤: ..."
    assert result.source == "https://github.com/acme/widget/issues/42"


def test_unsupported_url_is_rejected_before_any_network_call() -> None:
    def fail_if_called(url: str) -> str:
        raise AssertionError("不该发起请求")

    with pytest.raises(UnsupportedTicketUrl):
        import_ticket("https://jira.example.com/browse/AB-1", http_get=fail_if_called)


def test_missing_body_defaults_to_empty_string() -> None:
    def fake_get(url: str) -> str:
        return json.dumps({"title": "无正文"})

    result = import_ticket("https://github.com/acme/widget/issues/1", http_get=fake_get)

    assert result.body == ""
