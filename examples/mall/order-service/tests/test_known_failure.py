"""一个故意失败的测试。

它的存在是为了让后续 PRD 能演示"门禁失败 → 状态机回退 → 自动修复"这条链路，
而不必临时构造一个坏掉的仓库。默认被 `known-failure` 标记排除在门禁之外，
需要演示时把 `-m 'not known_failure'` 去掉即可。
"""

import pytest

pytestmark = pytest.mark.known_failure


def test_reserve_timeout_is_retried():
    # 尚未实现：预占超时后应当重试一次再放弃。
    assert False, "预占超时重试尚未实现"
