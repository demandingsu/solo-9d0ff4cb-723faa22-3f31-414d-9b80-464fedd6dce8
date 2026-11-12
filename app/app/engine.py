"""公告链还原与停运有效性判定。

规则（对应业务约定）：

* 每条公告按编号唯一标识；更正/撤销公告通过“引用编号”指向上一条公告，
  更正也可以引用另一条更正，从而形成链。链沿引用方向还原：原始公告为根，
  更正/撤销为后继；同一节点存在多个后继时按发布时间排序。
* 更正公告携带的停运起止（及线路）替换其之前链上的停运信息；
  链上最后一条非撤销公告的信息即当前生效信息。
* 撤销公告使它所在的整条链失效。
* 查询时刻之后才发布的公告在该时刻尚不可见，不参与判定，也不得作为来源列出。
* 更正/撤销引用了查询时刻尚未发布的公告时，该更正无法核实其引用依据，
  相关停运标为“待核实”（pending_verification）：更正不得脱离引用单独生效，
  被引用的未来公告不进入链与来源列表。
* 被引用公告在查询时刻之后才发布时，更正客观上早于原公告发布；查询时刻越过
  原公告发布时间后，这一发布时间矛盾仍然保留（publication_order_conflict），
  不倒置修订先后去制造“有效”结论。
* 同一起始公告若出现分叉后继，每个分支独立成链、独立判定；
  不同根公告（同线路的独立公告）天然分别判断。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .models import (
    ActiveSuspension,
    Announcement,
    InactiveChain,
    NoticeKind,
    UnmatchedReference,
)


@dataclass
class ReconstructionResult:
    active: list[ActiveSuspension]
    inactive: list[InactiveChain]
    unmatched: list[UnmatchedReference]
    # 编号 -> 公告（便于测试/诊断）
    by_id: dict[str, Announcement]


def reconstruct(
    announcements: list[Announcement], query_time: datetime
) -> ReconstructionResult:
    """根据引用关系与发布时间还原公告链并判定有效性。"""
    by_id: dict[str, Announcement] = {}
    dupes: set[str] = set()
    for a in announcements:
        if a.notice_id in by_id:
            # 同编号重复抓取：保留发布时间一致的第一条，其余忽略
            dupes.add(a.notice_id)
            continue
        by_id[a.notice_id] = a

    # 1) 无法匹配的引用：被引用编号在整站公告中不存在
    unmatched: list[UnmatchedReference] = []
    for a in announcements:
        for ref in a.references:
            if ref not in by_id:
                unmatched.append(
                    UnmatchedReference(
                        notice_id=a.notice_id, missing_reference=ref, url=a.url, kind=a.kind
                    )
                )

    visible = {nid for nid, a in by_id.items() if a.published_at <= query_time}

    # 2) 后继索引：引用本公告的可见公告（链上的后继）。指向未来公告的引用
    #    不挂边，因此更正/撤销在其依据尚未发布时仍是一条链的末端（叶子）。
    successors: dict[str, list[str]] = {nid: [] for nid in by_id}
    for a in by_id.values():
        if a.notice_id not in visible:
            continue
        for ref in a.references:
            if ref in visible:
                successors[ref].append(a.notice_id)
    for kids in successors.values():
        kids.sort(key=lambda cid: (by_id[cid].published_at, cid))

    # 3) 对每条公告区分：
    #    visible_refs —— 查询时刻已发布且存在的引用（链上的前驱/父节点）；
    #    future_refs  —— 查询时刻尚未发布的现存引用（待核实依据，不进入链）。
    def split_refs(a: Announcement) -> tuple[list[str], list[str]]:
        visible_refs: list[str] = []
        future_refs: list[str] = []
        for ref in a.references:
            if ref not in by_id:
                continue  # 悬空引用已在 unmatched 中报告
            if by_id[ref].published_at <= query_time:
                visible_refs.append(ref)
            else:
                future_refs.append(ref)
        return visible_refs, future_refs

    active: list[ActiveSuspension] = []
    inactive: list[InactiveChain] = []
    # 被可见公告引用的未来公告：已通过“待核实”链解释，不再独立成链
    consumed_future: set[str] = set()

    def walk(path: list[str], seen: set[str]) -> None:
        # 从链末端公告沿引用方向向上还原，翻转为“根在前、后继在后”的链顺序
        tail = path[-1]
        refs, _future = split_refs(by_id[tail])
        refs = [r for r in refs if r not in seen]
        refs.sort(key=lambda rid: (by_id[rid].published_at, rid))
        if not refs:
            evaluate_chain(list(reversed(path)))
            return
        for ref in refs:
            walk(path + [ref], seen | {ref})

    # 4) 叶子 = 查询时刻可见、且没有可见后继引用它的公告。每条引用链从其
    #    末端开始还原；指向未来公告的边不挂接，相关更正以叶子身份进入待核实。
    leaves = [nid for nid in visible if not successors[nid]]
    leaves.sort(key=lambda nid: (by_id[nid].published_at, nid))

    def evaluate_chain(chain_ids: list[str]) -> None:
        chain_ann = [by_id[i] for i in chain_ids]  # 全部为查询时刻可见公告
        sources = [a.url for a in chain_ann]
        root = chain_ann[0]
        latest_pub = max(a.published_at for a in chain_ann)

        # 4a) 链上任何公告引用了查询时刻尚未发布的公告 -> 待核实。
        #     未来公告不得进入 chain/sources，更正也不能脱离引用单独生效。
        pending_refs: list[str] = []
        for a in chain_ann:
            for ref in a.references:
                if (
                    ref in by_id
                    and by_id[ref].published_at > query_time
                    and ref not in pending_refs
                ):
                    pending_refs.append(ref)
                    consumed_future.add(ref)
        if pending_refs:
            inactive.append(
                InactiveChain(
                    root_id=root.notice_id,
                    route=_current_route(chain_ann),
                    reason="pending_verification",
                    chain=chain_ids,
                    sources=sources,
                    latest_published_at=latest_pub,
                    pending_references=pending_refs,
                )
            )
            return

        # 4b) 发布时间矛盾：后继公告（更正/撤销）早于它引用的已发布公告。
        #     即使查询时刻已越过双方发布时间，矛盾仍保留，不倒置修订先后。
        conflicts: list[str] = []
        for a in chain_ann:
            for ref in a.references:
                if ref in by_id and by_id[ref].published_at > a.published_at:
                    conflicts.append(f"{a.notice_id} 早于其引用的 {ref}")
        if conflicts:
            inactive.append(
                InactiveChain(
                    root_id=root.notice_id,
                    route=_current_route(chain_ann),
                    reason="publication_order_conflict",
                    chain=chain_ids,
                    sources=sources,
                    latest_published_at=latest_pub,
                    order_conflicts=conflicts,
                )
            )
            return

        # 撤销 -> 整条链失效（只统计查询时刻已发布的撤销）
        cancels = [
            a
            for a in chain_ann
            if a.kind is NoticeKind.CANCELLATION and a.published_at <= query_time
        ]
        if cancels:
            inactive.append(
                InactiveChain(
                    root_id=root.notice_id,
                    route=_current_route(chain_ann),
                    reason="cancelled",
                    chain=chain_ids,
                    sources=sources,
                    latest_published_at=cancels[-1].published_at,
                )
            )
            return

        # 按发布时间依次应用更正，后者替换前者的停运信息
        route, start, end, latest_published = _current_interval(chain_ann, query_time)
        if start is None or end is None:
            inactive.append(
                InactiveChain(
                    root_id=root.notice_id,
                    route=route,
                    reason="no_interval",
                    chain=chain_ids,
                    sources=sources,
                    latest_published_at=latest_published,
                )
            )
            return

        if start > query_time:
            reason = "not_yet_effective"
        elif end <= query_time:
            # 半开区间 [start, end)：查询时刻恰为结束时刻时已失效
            reason = "expired"
        else:
            reason = None

        if reason:
            inactive.append(
                InactiveChain(
                    root_id=root.notice_id,
                    route=route,
                    reason=reason,
                    chain=chain_ids,
                    sources=sources,
                    latest_published_at=latest_published,
                )
            )
        else:
            active.append(
                ActiveSuspension(
                    route=route or "(未知线路)",
                    suspend_start=start,
                    suspend_end=end,
                    source_chain=chain_ids,
                    sources=sources,
                    published_at=latest_published,
                )
            )

    def _current_route(chain_ann: list[Announcement]) -> str | None:
        route = None
        for a in sorted(chain_ann, key=lambda x: x.published_at):
            if a.published_at > query_time:
                continue
            if a.route:
                route = a.route
        return route

    def _current_interval(
        chain_ann: list[Announcement], now: datetime
    ) -> tuple[str | None, datetime | None, datetime | None, datetime | None]:
        route = None
        start = end = None
        latest_pub = None
        for a in sorted(chain_ann, key=lambda x: x.published_at):
            if a.published_at > now:
                continue
            latest_pub = a.published_at
            if a.route:
                route = a.route
            if a.suspend_start is not None:
                start = a.suspend_start
            if a.suspend_end is not None:
                end = a.suspend_end
        return route, start, end, latest_pub

    # 仅遍历查询时刻可见的叶子；不可见节点之间的边在该时刻不产生结论
    for leaf_id in leaves:
        walk([leaf_id], {leaf_id})

    # 5) 未被任何可见公告引用的未来公告：在查询时刻尚不可见，
    #    独立标记为 not_yet_effective（保持既有时间门控语义）
    for nid, a in by_id.items():
        if nid in visible or nid in consumed_future:
            continue
        inactive.append(
            InactiveChain(
                root_id=nid,
                route=a.route,
                reason="not_yet_effective",
                chain=[nid],
                sources=[a.url],
                latest_published_at=a.published_at,
            )
        )

    active.sort(key=lambda x: (x.route, x.suspend_start))
    inactive.sort(key=lambda x: (x.root_id, tuple(x.chain)))
    unmatched.sort(key=lambda x: (x.notice_id, x.missing_reference))

    return ReconstructionResult(
        active=active, inactive=inactive, unmatched=unmatched, by_id=by_id
    )
