from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def test_master_webui_config_encodes_tight_web_search_defaults():
    config = yaml.safe_load((ROOT / "master_webui_config.yaml").read_text(encoding="utf-8"))
    search = config["search"]

    assert search["ENABLE_WEB_SEARCH"] is True
    assert search["WEB_SEARCH_ENGINE"] == "searxng"
    assert search["WEB_SEARCH_TRUST_ENV"] is False
    assert search["WEB_SEARCH_RESULT_COUNT"] == 3
    assert search["WEB_SEARCH_CONCURRENT_REQUESTS"] == 1
    assert search["WEB_LOADER_ENGINE"] == "safe_web"
    assert search["WEB_LOADER_CONCURRENT_REQUESTS"] == 1
    assert search["SEARXNG_QUERY_URL"] == "http://searxng:8080/search"
    assert search["SEARXNG_LANGUAGE"] == "en"
    assert search["WEB_SEARCH_DOMAIN_FILTER_LIST"] == [
        "!cn",
        "!baidu.com",
        "!zhihu.com",
        "!weibo.com",
        "!sina.com.cn",
        "!csdn.net",
        "!163.com",
    ]


def test_open_webui_compose_defaults_enable_web_search():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert 'ENABLE_WEB_SEARCH: "true"' in compose
    assert 'WEB_SEARCH_ENGINE: "searxng"' in compose
    assert 'WEB_SEARCH_TRUST_ENV: "false"' in compose
    assert 'WEB_SEARCH_RESULT_COUNT: "3"' in compose
    assert 'WEB_SEARCH_CONCURRENT_REQUESTS: "1"' in compose
    assert 'WEB_LOADER_ENGINE: "safe_web"' in compose
    assert 'WEB_LOADER_CONCURRENT_REQUESTS: "1"' in compose
    assert 'SEARXNG_LANGUAGE: "en"' in compose


def test_config_drift_baseline_tracks_enabled_web_search():
    baseline = yaml.safe_load((ROOT / "config" / "config-drift-baseline.yaml").read_text(encoding="utf-8"))
    rule = next(item for item in baseline["rules"] if item["id"] == "admin.web_search.enabled")
    assert rule["expected"] is True
