"""测试公共 fixture"""
import os
import pytest


@pytest.fixture(autouse=True)
def _clean_idempotent_log():
    """每个测试结束后清理幂等日志，确保跨测试 / 跨运行隔离

    server.py 的 /api/sync 把 client_request_id 持久化到
    logs/sync/idempotent_{date}.json 用于幂等去重。
    测试用例硬编码 request_id，二次运行命中缓存会缺 success/errors 字段。
    """
    yield
    sync_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "logs", "sync",
    )
    if not os.path.exists(sync_dir):
        return
    for f in os.listdir(sync_dir):
        if f.startswith("idempotent_"):
            try:
                os.remove(os.path.join(sync_dir, f))
            except OSError:
                pass
