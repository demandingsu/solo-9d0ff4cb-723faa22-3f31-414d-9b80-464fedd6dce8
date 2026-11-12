"""端到端：FastAPI + 爬虫 + 引擎，对本地示例公告站做完整查询。

前置：示例站点已在 127.0.0.1:8080 提供服务（见 README / demo 脚本）。
"""
from __future__ import annotations

import os

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app

SITE = os.environ.get("TEST_SITE_URL", "http://127.0.0.1:8080/index.html")
Q = "2026-11-11T10:00:00+08:00"

pytestmark = pytest.mark.asyncio


async def test_full_query_workflow():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        # lifespan 由 LifespanManager 之外的方式不保证执行，手动触发
        async with app.router.lifespan_context(app):
            r = await ac.post("/query", json={"index_url": SITE, "query_time": Q})
    assert r.status_code == 200, r.text
    data = r.json()

    # 四个列表页全部爬到，16 条同站详情（跨站 1 条被跳过）
    assert len(data["list_pages_crawled"]) == 4
    assert data["detail_pages_crawled"] == 16
    assert data["announcements_parsed"] == 16
    assert len(data["skipped_offsite_links"]) == 1

    active = data["active_suspensions"]
    chains = {tuple(a["source_chain"]) for a in active}
    # 有效链：001 原始；005 原始（012 次日更正不可见）；
    # 003→006→010 两次更正；009→011 更正（带悬空引用）；
    # 016 与 014/015 同线路但相互独立，照常有效
    assert ("N-2026-001",) in chains
    assert ("N-2026-005",) in chains
    assert ("N-2026-003", "N-2026-006", "N-2026-010") in chains
    assert ("N-2026-009", "N-2026-011") in chains
    assert ("N-2026-016",) in chains

    by_route = {a["route"]: a for a in active}
    # 两次更正后的最终区间
    assert by_route["25 路"]["suspend_end"].startswith("2026-11-20T22:00:00")
    # 同线路两条独立公告分别判断：12 路有两条有效停运
    routes_12 = [a for a in active if a["route"] == "12 路"]
    assert len(routes_12) == 2

    reasons = {(c["root_id"], c["reason"]) for c in data["inactive_chains"]}
    assert ("N-2026-002", "expired") in reasons
    assert ("N-2026-004", "cancelled") in reasons
    assert ("N-2026-008", "not_yet_effective") in reasons
    assert ("N-2026-013", "not_yet_effective") in reasons

    # 先更正（015）后原公告（014）：查询时刻 014 尚未发布 -> 待核实，
    # 014 不得作为来源，015 也不能脱离引用单独报为有效停运
    pending = next(
        c for c in data["inactive_chains"]
        if c["reason"] == "pending_verification" and c["root_id"] == "N-2026-015"
    )
    assert pending["chain"] == ["N-2026-015"]
    assert pending["sources"] == [
        "http://127.0.0.1:8080/notices/N-2026-015.html"
    ]
    assert pending["pending_references"] == ["N-2026-014"]
    assert all("N-2026-014" not in c["chain"] for c in data["inactive_chains"])
    assert all(
        "N-2026-014" not in u
        for c in data["inactive_chains"]
        for u in c["sources"]
    )
    assert ("N-2026-014",) not in chains
    # 待核实更正自身也没有被报成有效停运
    assert ("N-2026-015",) not in chains
    # 被引用的未来原公告不再单独成链
    assert all(c["root_id"] != "N-2026-014" for c in data["inactive_chains"])

    unmatched = data["unmatched_references"]
    assert len(unmatched) == 1
    assert unmatched[0]["notice_id"] == "N-2026-011"
    assert unmatched[0]["missing_reference"] == "N-2026-999"

    # 来源链接完整
    chain25 = next(a for a in active if a["route"] == "25 路")
    assert all(u.startswith("http://127.0.0.1:8080/notices/") for u in chain25["sources"])


async def test_correction_before_original_after_publication_is_conflict():
    # 查询时刻越过原公告 014 的发布时间：双方均可见，但 015 仍早于 014，
    # 保留发布时间矛盾，不倒置修订先后制造有效结论
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        async with app.router.lifespan_context(app):
            r = await ac.post(
                "/query",
                json={
                    "index_url": SITE,
                    "query_time": "2026-11-15T10:00:00+08:00",
                },
            )
    assert r.status_code == 200, r.text
    data = r.json()

    conflict = next(c for c in data["inactive_chains"] if c["root_id"] == "N-2026-014")
    assert conflict["reason"] == "publication_order_conflict"
    # 原公告为根、更正为后继；发布时间矛盾保留，链顺序不倒置
    assert conflict["chain"] == ["N-2026-014", "N-2026-015"]
    assert conflict["sources"] == [
        "http://127.0.0.1:8080/notices/N-2026-014.html",
        "http://127.0.0.1:8080/notices/N-2026-015.html",
    ]
    assert conflict["order_conflicts"] == ["N-2026-015 早于其引用的 N-2026-014"]
    assert conflict["pending_references"] == []

    active_chains = {tuple(a["source_chain"]) for a in data["active_suspensions"]}
    assert ("N-2026-014", "N-2026-015") not in active_chains
    assert ("N-2026-015",) not in active_chains


async def test_naive_query_time_rejected():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        async with app.router.lifespan_context(app):
            r = await ac.post(
                "/query",
                json={"index_url": SITE, "query_time": "2026-11-11T10:00:00"},
            )
    assert r.status_code == 422


async def test_bad_index_url_returns_502():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        async with app.router.lifespan_context(app):
            r = await ac.post(
                "/query",
                json={
                    "index_url": "http://127.0.0.1:9/never.html",
                    "query_time": Q,
                },
            )
    assert r.status_code == 502
