"""航段占用冲突测试：已确认船舶不得在同一航段同一分钟重叠。

规则要点：
* 每段行程于进入分钟 d 独占该航段 d..d+t-1（抵达分钟 d+t 不占用）；
* 已确认快照为不可变历史，即使彼此重叠也原样保留，后续规划必须避开；
* 等待发生在节点上，不占用航段；
* 确认须在同一事务内按当时占用重算，占用导致提交行程不再最优 -> PLAN_MISMATCH；
* 复核原快照只查潮位（不查占用）；改期建议避开其他快照但排除自身。
"""
import json

from app.database import connect

from conftest import add_node, add_segment, seed_initial_revision


def _level(lo, hi, v):
    return {m: v for m in range(lo, hi + 1)}


def _plan(client, rev, origin="A", destination="B", start=0, window=20,
          draft=1.0, height=0.0, margin=0.0):
    r = client.post("/api/trips/plan", json={
        "origin": origin, "destination": destination,
        "start_minute": start, "window_minutes": window,
        "draft": draft, "height": height, "margin": margin,
        "revision": rev,
    })
    assert r.status_code == 200, r.text
    return r.json()


def _confirm(client, planned):
    r = client.post("/api/trips/confirm", json=planned)
    assert r.status_code == 201, r.text
    return r.json()


def _insert_snapshot(rev, origin="A", destination="B", start=0, window=20,
                     draft=1.0, height=0.0, margin=0.0, itinerary=None,
                     arrival=None):
    """直接写入一条已确认快照（模拟历史/升级前数据，彼此可能重叠）。"""
    if itinerary is None:
        itinerary = []
    if arrival is None:
        arrival = itinerary[-1]["end_minute"] if itinerary else start
    conn = connect()
    try:
        trip_id = conn.execute(
            "INSERT INTO confirmed_trips"
            " (revision, origin, destination, start_minute, window_minutes,"
            "  draft, height, margin, arrival_minute, itinerary)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (rev, origin, destination, start, window, draft, height, margin,
             arrival, json.dumps(itinerary)),
        ).lastrowid
    finally:
        conn.close()
    return trip_id


def _leg(seg_id, origin, destination, dep, t):
    return {"seg_id": seg_id, "origin": origin, "destination": destination,
            "start_minute": dep, "end_minute": dep + t, "travel_time": t}


# ---------------------------------------------------------------------------
# 1. 规划：后续船舶须避开已确认快照对同航段的占用（抵达分钟可被下一段使用）
# ---------------------------------------------------------------------------

def test_plan_avoids_confirmed_occupancy(client):
    add_node(client, "A"); add_node(client, "B")
    seg = add_segment(client, "A", "B", travel_time=2, depth=99.0)
    rev = seed_initial_revision(_level(0, 19, 3.0))

    # 第一艘：0 进入，占用 0、1，第 2 分钟抵达
    t1 = _confirm(client, _plan(client, rev))
    assert [l["start_minute"] for l in t1["itinerary"]] == [0]

    # 第二艘：0、1 被占，最早第 2 分钟进入（上一艘的抵达分钟不占用）
    p2 = _plan(client, rev)
    assert p2["itinerary"] == [_leg(seg, "A", "B", 2, 2)]
    t2 = _confirm(client, p2)

    # 第三艘：0..3 被占，最早第 4 分钟进入
    p3 = _plan(client, rev)
    assert p3["itinerary"] == [_leg(seg, "A", "B", 4, 2)]


def test_plan_occupancy_blocked_whole_window(client):
    add_node(client, "A"); add_node(client, "B")
    add_segment(client, "A", "B", travel_time=2, depth=99.0)
    rev = seed_initial_revision(_level(0, 19, 3.0))

    # 已确认快照占用 0、1；窗口仅 3 分钟（0..2）：进入 0 占(0,1)、进入 1 占(1,2)
    # 均冲突 -> 窗口内无可行行程
    _insert_snapshot(rev, itinerary=[_leg(1, "A", "B", 0, 2)])
    r = client.post("/api/trips/plan", json={
        "origin": "A", "destination": "B",
        "start_minute": 0, "window_minutes": 3,
        "draft": 1.0, "height": 0.0, "margin": 0.0, "revision": rev,
    })
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "NO_FEASIBLE_ITINERARY"


def test_occupancy_does_not_block_different_segment(client):
    for n in ["A", "B", "X", "Y"]:
        add_node(client, n)
    s1 = add_segment(client, "A", "B", travel_time=2, depth=99.0)
    s2 = add_segment(client, "X", "Y", travel_time=2, depth=99.0)
    rev = seed_initial_revision(_level(0, 19, 3.0))

    t1 = _confirm(client, _plan(client, rev))  # 占 s1 的 0、1
    # 不同航段同时刻不冲突
    other = _plan(client, rev, origin="X", destination="Y")
    assert [l["seg_id"] for l in other["itinerary"]] == [s2]
    assert other["itinerary"][0]["start_minute"] == 0
    assert s1 != s2


# ---------------------------------------------------------------------------
# 2. 节点等待不占用航段：他船在节点等待不影响本船使用该航段
# ---------------------------------------------------------------------------

def test_waiting_at_node_does_not_consume_segment(client):
    # s1 A->B t=1 全程可行；s2 B->C t=1 仅第 5、6 分钟潮位足够。
    for n in ["A", "B", "C"]:
        add_node(client, n)
    s1 = add_segment(client, "A", "B", travel_time=1, depth=99.0)
    s2 = add_segment(client, "B", "C", travel_time=1, depth=4.0)
    levels = {**_level(0, 4, 0.0), 5: 3.0, 6: 3.0, **_level(7, 19, 0.0)}
    rev = seed_initial_revision(levels)

    body = dict(origin="A", destination="C", start_minute=0, window_minutes=20,
                draft=6.0, height=0.0, margin=0.5, revision=rev)
    t1 = _confirm(client, client.post("/api/trips/plan", json=body).json())
    # s2 只能在第 5 分钟走，第 6 分钟抵达（s1 具体出发分钟不影响本断言）
    assert [(l["seg_id"], l["start_minute"]) for l in t1["itinerary"]][-1] == (s2, 5)
    s1_dep = t1["itinerary"][0]["start_minute"]

    # 占用只含实际航行分钟：船1 在 B 的等待分钟（s1_dep+1..4）不占 s2
    occ = client.get("/api/occupancy", params={"from_minute": 0, "to_minute": 6}).json()
    occupied_pairs = {(e["seg_id"], e["minute"]) for e in occ["occupancy"]}
    assert (s2, 5) in occupied_pairs
    assert all((s2, m) not in occupied_pairs for m in range(0, 5))
    assert (s1, s1_dep) in occupied_pairs

    # 第二艘：s2 的 5 被占只能 6 出发（船1 在 B 等待不碍事），最早第 7 分钟抵达；
    # s1 出发分钟在同序列/同抵达下不唯一，这里只断言占用约束相关结论。
    p2 = client.post("/api/trips/plan", json=body).json()
    assert p2["arrival_minute"] == 7
    assert [(l["seg_id"], l["start_minute"], l["end_minute"]) for l in p2["itinerary"]][1] == \
        (s2, 6, 7)
    s1_leg = p2["itinerary"][0]
    assert s1_leg["seg_id"] == s1
    assert s1_leg["start_minute"] != s1_dep  # 避开船1 的 s1 实际占用分钟


# ---------------------------------------------------------------------------
# 3. 确认：同一事务按当时占用重算；提交行程不再最优 -> PLAN_MISMATCH，冲突不保存
# ---------------------------------------------------------------------------

def test_confirm_recalculates_with_current_occupancy(client):
    add_node(client, "A"); add_node(client, "B")
    add_segment(client, "A", "B", travel_time=2, depth=99.0)
    rev = seed_initial_revision(_level(0, 19, 3.0))

    # 两艘船在任一方确认前各自规划：都得到第 0 分钟出发
    planned_a = _plan(client, rev)
    planned_b = _plan(client, rev)
    assert planned_a["itinerary"][0]["start_minute"] == 0
    assert planned_b["itinerary"][0]["start_minute"] == 0

    # 先确认 A：占用 0、1
    _confirm(client, planned_a)

    # 再用旧规划结果确认 B：修订未过期，但占用已变、重算结果为第 2 分钟出发
    r = client.post("/api/trips/confirm", json=planned_b)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "PLAN_MISMATCH"
    # 冲突不保存
    assert [t["trip_id"] for t in client.get("/api/trips").json()] == [1]

    # 用最新占用重新规划后可正常确认
    fresh = _plan(client, rev)
    assert fresh["itinerary"][0]["start_minute"] == 2
    _confirm(client, fresh)
    assert len(client.get("/api/trips").json()) == 2


# ---------------------------------------------------------------------------
# 4. 既有已确认快照即使彼此重叠也原样保留；后续规划仍须避开全部占用
# ---------------------------------------------------------------------------

def test_overlapping_legacy_snapshots_are_kept_and_blocking(client):
    add_node(client, "A"); add_node(client, "B")
    seg = add_segment(client, "A", "B", travel_time=2, depth=99.0)
    rev = seed_initial_revision(_level(0, 19, 3.0))

    # 两条历史快照重叠占用 0、1（模拟本轮功能上线前已存在的数据）
    id1 = _insert_snapshot(rev, itinerary=[_leg(seg, "A", "B", 0, 2)])
    id2 = _insert_snapshot(rev, itinerary=[_leg(seg, "A", "B", 0, 2)])
    assert id1 != id2

    trips = client.get("/api/trips").json()
    assert len(trips) == 2  # 原样保留，不清洗、不拒绝

    # 新规划须避开重叠占用
    planned = _plan(client, rev)
    assert planned["itinerary"][0]["start_minute"] == 2

    # 占用查询同一 (seg, minute) 给出两个行程
    occ = client.get("/api/occupancy", params={"from_minute": 0, "to_minute": 3}).json()
    by_minute = {(e["seg_id"], e["minute"]): e["trip_ids"]
                 for e in occ["occupancy"]}
    assert by_minute[(seg, 0)] == [id1, id2]
    assert by_minute[(seg, 1)] == [id1, id2]
    assert (seg, 2) not in by_minute  # 抵达分钟不占用


# ---------------------------------------------------------------------------
# 5. 复核：原快照只查潮位不查占用（他船重叠仍 safe=true）
# ---------------------------------------------------------------------------

def test_review_ignores_occupancy_for_original_snapshot(client):
    add_node(client, "A"); add_node(client, "B")
    seg = add_segment(client, "A", "B", travel_time=2, depth=99.0)
    rev = seed_initial_revision(_level(0, 19, 3.0))

    trip = _confirm(client, _plan(client, rev))  # 占 0、1
    # 另一条历史快照与其重叠
    _insert_snapshot(rev, itinerary=[_leg(seg, "A", "B", 0, 2)])

    data = client.post(f"/api/trips/{trip['trip_id']}/review",
                       json={"target_revision": rev}).json()
    assert data["safe"] is True
    assert data["first_violation"] is None
    assert data["reschedule"] is None


# ---------------------------------------------------------------------------
# 6. 改期建议：避开其他已确认快照，但排除自身占用
# ---------------------------------------------------------------------------

def test_reschedule_avoids_others_but_excludes_self(client):
    add_node(client, "A"); add_node(client, "B")
    seg = add_segment(client, "A", "B", travel_time=2, depth=4.0)
    rev1 = seed_initial_revision(_level(0, 19, 3.0))  # 全程潮位足够

    # 本船快照：0 出发占 0、1
    trip = _confirm(client, _plan(client, rev1, draft=6.0, margin=0.5))
    assert trip["itinerary"] == [_leg(seg, "A", "B", 0, 2)]

    # 另一条已确认快照占用 3、4（历史数据，与本船不重叠）
    other = _insert_snapshot(
        rev1, draft=6.0, margin=0.5,
        itinerary=[_leg(seg, "A", "B", 3, 2)],
    )

    # 修订 #2：仅第 0 分钟低潮 -> 原行程不安全（首违第 0 分钟）
    rev2 = client.post("/api/tide/corrections",
                       json={"corrections": {0: 0.0}}).json()["revision"]

    data = client.post(f"/api/trips/{trip['trip_id']}/review",
                       json={"target_revision": rev2}).json()
    assert data["safe"] is False
    assert data["first_violation"]["minute"] == 0
    rs = data["reschedule"]
    assert rs is not None
    # d=1 占 1、2：分钟 1 是本船自身占用（排除），分钟 2 空闲 -> 可行且最早；
    # 若错误地把自身也算作占用，则会被推到第 5 分钟（3、4 被他船占用）。
    assert rs["itinerary"] == [_leg(seg, "A", "B", 1, 2)]
    assert rs["arrival_minute"] == 3
    assert rs["changed_leg_count"] == 1
    # 建议不与他船快照（3、4）冲突
    assert {1, 2}.isdisjoint({3, 4})
    assert other != trip["trip_id"]

    # 建议不占位、快照不改写
    saved = client.get(f"/api/trips/{trip['trip_id']}").json()
    assert saved["itinerary"] == trip["itinerary"]


def test_reschedule_no_solution_when_others_block_window(client):
    add_node(client, "A"); add_node(client, "B")
    seg = add_segment(client, "A", "B", travel_time=2, depth=4.0)
    rev1 = seed_initial_revision(_level(0, 19, 3.0))
    trip = _confirm(client, _plan(client, rev1, draft=6.0, margin=0.5))

    # 他船历史快照占 1..19；修订 #2 使第 0 分钟低潮：
    # d=0 潮位不可行，d>=1 与他船冲突 -> 窗口内无解
    _insert_snapshot(rev1, draft=6.0, margin=0.5,
                     itinerary=[_leg(seg, "A", "B", 1, 19)], arrival=20)
    rev2 = client.post("/api/tide/corrections",
                       json={"corrections": {0: 0.0}}).json()["revision"]

    data = client.post(f"/api/trips/{trip['trip_id']}/review",
                       json={"target_revision": rev2}).json()
    assert data["safe"] is False
    assert data["no_solution"] is True
    assert data["reschedule"] is None
    assert data["first_violation"]["minute"] == 0


# ---------------------------------------------------------------------------
# 7. 占用查询接口：区间过滤、按 seg/minute 升序、抵达分钟不占
# ---------------------------------------------------------------------------

def test_occupancy_endpoint_filters_and_orders(client):
    add_node(client, "A"); add_node(client, "B")
    seg = add_segment(client, "A", "B", travel_time=2, depth=99.0)
    rev = seed_initial_revision(_level(0, 19, 3.0))

    t1 = _confirm(client, _plan(client, rev))   # 占 0、1
    t2 = _confirm(client, _plan(client, rev))   # 占 2、3

    r = client.get("/api/occupancy", params={"from_minute": 2, "to_minute": 10})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["from_minute"] == 2 and data["to_minute"] == 10
    assert data["count"] == 2
    assert [(e["seg_id"], e["minute"], e["trip_ids"]) for e in data["occupancy"]] == [
        (seg, 2, [t2["trip_id"]]),
        (seg, 3, [t2["trip_id"]]),
    ]
    assert data["occupancy"][0]["origin"] == "A"
    assert data["occupancy"][0]["destination"] == "B"

    # 全区间按分钟升序
    full = client.get("/api/occupancy").json()
    assert [(e["seg_id"], e["minute"]) for e in full["occupancy"]] == \
        [(seg, 0), (seg, 1), (seg, 2), (seg, 3)]
    assert full["count"] == 4


def test_occupancy_endpoint_bad_range(client):
    r = client.get("/api/occupancy", params={"from_minute": 5, "to_minute": 2})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"
