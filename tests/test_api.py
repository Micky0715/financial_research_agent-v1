"""FastAPI service tests (v4 stage J/L): health + async task lifecycle.

WorkflowOrchestrator.run() 本身要真实的网络/LLM，不适合放进离线单测；这里
只 monkeypatch 掉它，验证 API 层自己的逻辑（提交/状态流转/结果映射/错误
处理）——核心业务逻辑（TaskManager 的状态判定、线程池调度、404/202 处理）
全部真实执行，只是没让它跑真正的 pipeline。
"""
import time

import api.task_manager as tm_module
from api.task_manager import TaskManager
from fastapi.testclient import TestClient


def _fast_orchestrator_run(monkeypatch, result: dict, delay: float = 0.05):
    """WorkflowOrchestrator().run(...) -> 立即返回给定 result（模拟一次真实 run 的产物）。"""
    class _FakeOrchestrator:
        def run(self, request, on_progress=None):
            if on_progress:
                on_progress(1, 5, "Searching sources...")
                time.sleep(delay)
                on_progress(5, 5, "Evaluating report...")
            return result

    monkeypatch.setattr(tm_module, "WorkflowOrchestrator", lambda: _FakeOrchestrator())


def _client_with_fresh_manager(monkeypatch) -> TestClient:
    """每个测试用独立 TaskManager，避免测试间任务状态串味。"""
    fresh = TaskManager(max_concurrent=2, task_timeout_seconds=10)
    monkeypatch.setattr(tm_module, "task_manager", fresh)
    import api.routes.reports as reports_module
    import api.routes.tasks as tasks_module

    monkeypatch.setattr(reports_module, "task_manager", fresh)
    monkeypatch.setattr(tasks_module, "task_manager", fresh)
    from api.main import app

    return TestClient(app)


def _wait_terminal(client: TestClient, task_id: str, timeout: float = 5.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = client.get(f"/tasks/{task_id}")
        data = r.json()
        if data["status"] in ("succeeded", "failed", "degraded"):
            return data
        time.sleep(0.05)
    raise AssertionError(f"task {task_id} did not reach a terminal status within {timeout}s")


def test_health_endpoint_reports_all_checks():
    from api.main import app

    client = TestClient(app)
    r = client.get("/health")
    assert r.status_code == 200
    data = r.json()
    assert data["status"] in ("healthy", "degraded", "unhealthy")
    names = {c["name"] for c in data["checks"]}
    assert {"api", "llm_provider", "mcp", "akshare", "embedding", "export_deps", "memory"} <= names


def test_create_report_and_poll_to_success(monkeypatch):
    _fast_orchestrator_run(monkeypatch, {
        "run_id": "testrun1", "report_path": "outputs/reports/testrun1_x_report.md",
        "trace_path": "x", "sources_path": "", "evaluation_path": "x",
        "evaluation": {"overall_score": 0.8, "diagnostics": {}}, "num_sources": 5, "duration": 1.2,
    })
    client = _client_with_fresh_manager(monkeypatch)

    r = client.post("/reports", json={"topic": "测试主题投资分析", "report_type": "company_research"})
    assert r.status_code == 200
    task_id = r.json()["task_id"]
    assert r.json()["status"] == "queued"

    status = _wait_terminal(client, task_id)
    assert status["status"] == "succeeded"
    assert status["progress"] == 1.0

    report = client.get(f"/reports/{task_id}")
    assert report.status_code == 200
    body = report.json()
    assert body["status"] == "succeeded"
    assert body["evaluation"]["overall_score"] == 0.8


def test_report_result_returns_202_while_running(monkeypatch):
    _fast_orchestrator_run(monkeypatch, {"run_id": "r2", "report_path": "p", "sources_path": "",
                                         "evaluation": {"overall_score": 0.8}, "num_sources": 5}, delay=0.3)
    client = _client_with_fresh_manager(monkeypatch)
    task_id = client.post("/reports", json={"topic": "测试主题"}).json()["task_id"]

    r = client.get(f"/reports/{task_id}")
    assert r.status_code == 202


def test_unknown_task_id_returns_404(monkeypatch):
    client = _client_with_fresh_manager(monkeypatch)
    assert client.get("/tasks/doesnotexist").status_code == 404
    assert client.get("/reports/doesnotexist").status_code == 404


def test_entity_validation_failure_maps_to_failed_status(monkeypatch):
    _fast_orchestrator_run(monkeypatch, {
        "run_id": "r3", "report_path": "p", "sources_path": "",
        "evaluation": {"overall_score": 0.0, "entity_validation_failed": True, "diagnostics": {}},
        "num_sources": 0,
    })
    client = _client_with_fresh_manager(monkeypatch)
    task_id = client.post("/reports", json={"topic": "虚构公司投资分析"}).json()["task_id"]
    status = _wait_terminal(client, task_id)
    assert status["status"] == "failed"
    assert status["error"] == "insufficient_entity_evidence"


def test_unsupported_unlisted_company_maps_to_degraded(monkeypatch):
    _fast_orchestrator_run(monkeypatch, {
        "run_id": "r4", "report_path": "p", "sources_path": "",
        "evaluation": {"overall_score": 0.6,
                       "diagnostics": {"unsupported_unlisted_company": True}},
        "num_sources": 3,
    })
    client = _client_with_fresh_manager(monkeypatch)
    task_id = client.post("/reports", json={"topic": "华为投资分析"}).json()["task_id"]
    status = _wait_terminal(client, task_id)
    assert status["status"] == "degraded"


def test_concurrent_task_limit_is_respected(monkeypatch):
    """max_concurrent=1 时两个任务应该顺序执行，不是同时 running。"""
    _fast_orchestrator_run(monkeypatch, {"run_id": "r5", "report_path": "p", "sources_path": "",
                                         "evaluation": {"overall_score": 0.8}, "num_sources": 5}, delay=0.2)
    fresh = TaskManager(max_concurrent=1, task_timeout_seconds=10)
    monkeypatch.setattr(tm_module, "task_manager", fresh)
    import api.routes.reports as reports_module
    import api.routes.tasks as tasks_module

    monkeypatch.setattr(reports_module, "task_manager", fresh)
    monkeypatch.setattr(tasks_module, "task_manager", fresh)
    from api.main import app

    client = TestClient(app)
    t1 = client.post("/reports", json={"topic": "主题一"}).json()["task_id"]
    t2 = client.post("/reports", json={"topic": "主题二"}).json()["task_id"]
    time.sleep(0.05)  # 让 t1 先进入 running
    s1 = client.get(f"/tasks/{t1}").json()["status"]
    s2 = client.get(f"/tasks/{t2}").json()["status"]
    assert s1 == "running"
    assert s2 == "queued", "第二个任务在并发上限=1时应仍排队，不应同时 running"
    _wait_terminal(client, t1)
    _wait_terminal(client, t2)
