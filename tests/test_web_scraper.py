"""Unit tests for webpage validation and HTML extraction."""
import pytest

from mcp.tools.web_scraper import (
    WebScraperError,
    _validate_public_url,
    extract_page_text,
    is_web_url,
)


def test_detects_single_web_url():
    assert is_web_url("https://jobs.example.com/ai-engineer") is True
    assert is_web_url("岗位链接 https://example.com/job") is False
    assert is_web_url("招聘AI应用工程师") is False


def test_rejects_local_targets():
    with pytest.raises(WebScraperError, match="本机或内网"):
        _validate_public_url("http://127.0.0.1:8000/private")


def test_extracts_main_content_and_removes_noise():
    html = """
    <html><head><title>AI应用工程师招聘</title><style>.x{}</style></head>
    <body>
      <nav>首页 招聘列表</nav>
      <main>
        <h1>AI应用工程师</h1>
        <p>负责大模型应用、RAG和智能体系统的设计与开发。</p>
        <p>要求熟悉Python、FastAPI、向量数据库和模型评估。</p>
        <p>能够完成需求分析、工程实现、测试和持续优化。</p>
      </main>
      <script>window.secret = 'noise';</script>
    </body></html>
    """

    result = extract_page_text(html, "https://example.com/job")

    assert result["title"] == "AI应用工程师招聘"
    assert "负责大模型应用" in result["text"]
    assert "首页 招聘列表" not in result["text"]
    assert "window.secret" not in result["text"]
