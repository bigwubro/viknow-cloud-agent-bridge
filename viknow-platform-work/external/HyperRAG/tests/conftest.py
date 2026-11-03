"""
测试配置文件
"""

import asyncio
import os

import pytest


def pytest_addoption(parser):
    """添加命令行选项"""
    parser.addoption(
        "--run-integration",
        action="store_true",
        default=False,
        help="运行集成测试（需要真实服务：Redis/Postgres/Neo4j/LLM）",
    )


def pytest_configure(config):
    """配置测试标记"""
    config.addinivalue_line(
        "markers", "integration: 集成测试，需要外部服务（Redis/Postgres/Neo4j/LLM/Embed）"
    )
    config.addinivalue_line("markers", "unit: 单元测试，不依赖外部服务")


def pytest_collection_modifyitems(config, items):
    """
    自动为测试添加标记，并跳过未开启的集成测试

    根据文件路径自动判断：
    - tests/unit/ 下的测试标记为 unit
    - tests/integration/ 下的测试标记为 integration
    """
    # 检查是否开启集成测试（命令行选项或环境变量）
    run_integration = (
        config.getoption("--run-integration")
        or os.getenv("HYPERRAG_RUN_INTEGRATION", "").lower() == "true"
    )

    skip_integration = pytest.mark.skip(
        reason="需要 --run-integration 或 HYPERRAG_RUN_INTEGRATION=true 来运行集成测试"
    )

    for item in items:
        # 根据文件路径判断测试类型
        file_path = str(item.fspath)
        is_integration_path = "/integration/" in file_path or file_path.endswith("/integration")
        is_unit_path = "/unit/" in file_path or file_path.endswith("/unit")

        # 如果路径明确，优先使用路径判断
        if is_integration_path:
            item.add_marker(pytest.mark.integration)
            if not run_integration:
                item.add_marker(skip_integration)
        elif is_unit_path:
            item.add_marker(pytest.mark.unit)
        else:
            # 回退到 marker 判断（兼容旧代码）
            if "integration" in [marker.name for marker in item.iter_markers()]:
                if not run_integration:
                    item.add_marker(skip_integration)
            else:
                # 默认所有测试都是单元测试
                item.add_marker(pytest.mark.unit)


@pytest.fixture(scope="session")
def test_config():
    """提供测试配置"""
    return {
        "base_url": os.getenv("HYPERRAG_TEST_BASE_URL", "http://dev.ai.qingxiang.tech:8080/v1"),
        "api_key": os.getenv("HYPERRAG_TEST_API_KEY", "QX123456"),
        "llm_model": os.getenv("HYPERRAG_TEST_LLM_MODEL", "Qwen/Qwen3-VL-30B-A3B-Instruct-FP8"),
        "embed_model": os.getenv("HYPERRAG_TEST_EMBED_MODEL", "Qwen/Qwen3-Embedding-0.6B"),
        "embedding_dim": int(os.getenv("HYPERRAG_TEST_EMBEDDING_DIM", "1024")),
    }


@pytest.fixture(scope="session")
def event_loop():
    """
    使用 session 级 event loop，确保 session 级 async fixture（如 shared_hyperrag）
    创建的后台任务（scheduler/heartbeat）不会因为用例切换 loop 而停止推进。
    """
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
def mineru_output_factory():
    """Write small MinerU contract outputs without running the external engine."""
    import json
    from pathlib import Path

    def write(output_dir, source_path):
        method_dir = Path(output_dir) / Path(source_path).stem / "vlm"
        method_dir.mkdir(parents=True, exist_ok=True)
        items = [
            {"type": "text", "text_level": 1, "text": "Equipment guide", "page_idx": 0},
            {
                "type": "text",
                "text": " ".join(f"instruction-{index}" for index in range(600)),
                "page_idx": 0,
            },
            {
                "type": "image",
                "img_path": "images/indicator.png",
                "img_caption": ["Power indicator"],
                "page_idx": 0,
            },
            {
                "type": "table",
                "img_path": "images/indicator.png",
                "table_body": "<table><tr><td>Voltage</td><td>220 V</td></tr></table>",
                "table_caption": ["Operating limits"],
                "page_idx": 1,
            },
            {
                "type": "text",
                "text": "Stop the machine after completing the procedure.",
                "page_idx": 1,
            },
        ]
        (method_dir / "images").mkdir(exist_ok=True)
        (method_dir / "images" / "indicator.png").write_bytes(b"image fixture")
        (method_dir / "sample_content_list.json").write_text(json.dumps(items), encoding="utf-8")
        (method_dir / "sample_middle.json").write_text('{"pdf_info": []}', encoding="utf-8")
        return method_dir

    return write


@pytest.fixture
def stub_mineru_command(monkeypatch, mineru_output_factory):
    from hyperrag.parser import MinerUParser

    calls = []

    def run(input_path, output_dir, *args, **kwargs):
        calls.append((input_path, output_dir))
        mineru_output_factory(output_dir, input_path)

    monkeypatch.setattr(MinerUParser, "_run_mineru_command", staticmethod(run))
    return calls
