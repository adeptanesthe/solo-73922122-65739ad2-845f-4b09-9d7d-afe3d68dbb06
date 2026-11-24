"""核心算法 + 分轮核定网页自测。

运行：python3 tests.py
"""
import io as _io
import itertools
import random

import app as web
import core
from core import (compute_alerts, counterexample, max_matching, min_hits,
                  solve)
from sample_data import to_csv


def brute_max_matching(combo, alerts, L, U):
    """增广路求二分图最大匹配（暴力，供对照）。"""
    adj = []
    for t in combo:
        lo, hi = t - U, t - L
        adj.append([a for a in alerts if lo <= a <= hi])
    match_of_alert = {}

    def aug(i, seen):
        for a in adj[i]:
            if a in seen:
                continue
            seen.add(a)
            if a not in match_of_alert or aug(match_of_alert[a], seen):
                match_of_alert[a] = i
                return True
        return False

    cnt = 0
    for i in range(len(combo)):
        if aug(i, set()):
            cnt += 1
    return cnt


def brute_min_hits(L, alerts, intervals, U):
    """枚举全部时刻组合，取最大配对数的最小值（暴力，供对照）。"""
    best = None
    for combo in itertools.product(*[range(a, b + 1) for a, b in intervals]):
        v = brute_max_matching(list(combo), alerts, L, U)
        best = v if best is None else min(best, v)
    return best


def test_compute_alerts():
    minutes = [10, 11, 12, 13, 14, 15, 16, 17]
    scores = [5, 9, 9, 8, 2, 8, 7, 1]
    assert compute_alerts(minutes, scores, 8) == [11, 15]
    assert compute_alerts(minutes, scores, 10) == []
    assert compute_alerts(minutes, scores, 1) == [10]  # 整段只在首分钟告警
    print("ok  compute_alerts")


def test_max_matching_vs_brute(rng=None, trials=300):
    if rng is None:
        rng = random.Random(101)
    for _ in range(trials):
        n = rng.randint(0, 12)
        alerts = sorted(rng.sample(range(0, 40), n))
        m = rng.randint(1, 5)
        U = rng.randint(1, 8)
        L = rng.randint(1, U)
        combo = [rng.randint(0, 40) for _ in range(m)]
        g = len(max_matching(combo, alerts, L, U))
        b = brute_max_matching(combo, alerts, L, U)
        assert g == b, (combo, alerts, L, U, g, b)
    print("ok  max_matching == brute_max_matching")


def test_min_hits_vs_brute(rng=None, trials=400):
    if rng is None:
        rng = random.Random(202)
    for _ in range(trials):
        n = rng.randint(0, 10)
        alerts = sorted(rng.sample(range(0, 30), n))
        m = rng.randint(1, 4)
        U = rng.randint(1, 7)
        L = rng.randint(1, U)
        # 互不重叠的小区间
        intervals = []
        cur = rng.randint(0, 3)
        for _ in range(m):
            a = cur
            b = a + rng.randint(0, 3)
            intervals.append((a, b))
            cur = b + rng.randint(1, 4)
        got = min_hits(L, alerts, intervals, U)
        want = brute_min_hits(L, alerts, intervals, U)
        assert got == want, (L, alerts, intervals, U, got, want)
        # 反例组合的配对数应恰好等于 min_hits
        combo, pairs = counterexample(L, alerts, intervals, U)
        assert len(pairs) == want, (L, alerts, intervals, U, combo, pairs, want)
        # 反例组合必须取自各区间
        for (a, b), t in zip(intervals, combo):
            assert a <= t <= b
    print("ok  min_hits/counterexample == brute force (400 组随机)")


def test_solve_vs_linear(rng=None, trials=120):
    if rng is None:
        rng = random.Random(303)
    for _ in range(trials):
        n = rng.randint(0, 10)
        alerts = sorted(rng.sample(range(0, 30), n))
        m = rng.randint(1, 4)
        U = rng.randint(1, 8)
        K = rng.randint(1, m + 2)
        intervals = []
        cur = rng.randint(0, 3)
        for _ in range(m):
            a = cur
            b = a + rng.randint(0, 3)
            intervals.append((a, b))
            cur = b + rng.randint(1, 4)
        res = solve(alerts, intervals, U, K)
        # 线性扫描对照
        mh = {L: brute_min_hits(L, alerts, intervals, U) for L in range(1, U + 1)}
        feasible = [L for L in range(1, U + 1) if mh[L] >= K]
        if not feasible:
            assert res["status"] == "infeasible", (res, mh, K)
            c = res["counter"]
            assert c["L"] == 1 and c["hits"] == mh[1] < K
        else:
            Lstar = max(feasible)
            assert res["status"] == "ok" and res["L"] == Lstar, (res, mh, K)
            assert res["min_hits"] == mh[Lstar]
            if Lstar < U:
                c = res["counter"]
                assert c["L"] == Lstar + 1 and c["hits"] == mh[Lstar + 1] < K
            else:
                assert "counter" not in res
    print("ok  solve == 线性扫描暴力解 (120 组随机)")


def test_monotonicity(rng=None, trials=60):
    if rng is None:
        rng = random.Random(404)
    for _ in range(trials):
        n = rng.randint(0, 12)
        alerts = sorted(rng.sample(range(0, 40), n))
        m = rng.randint(1, 5)
        U = rng.randint(1, 10)
        intervals = []
        cur = rng.randint(0, 3)
        for _ in range(m):
            a = cur
            b = a + rng.randint(0, 3)
            intervals.append((a, b))
            cur = b + rng.randint(1, 4)
        seq = [min_hits(L, alerts, intervals, U) for L in range(1, U + 1)]
        assert all(seq[i] >= seq[i + 1] for i in range(len(seq) - 1)), seq
    print("ok  min_hits 关于 L 单调不增")


def test_edge_cases():
    # 无告警 -> 无法承诺
    res = solve([], [(10, 12)], 5, 1)
    assert res["status"] == "infeasible" and res["counter"]["hits"] == 0
    # K 大于卡纸数 -> 无法承诺（告警 6/7/8 能覆盖 [10,12] 的任一时刻，但最多配对 1 次）
    res = solve([6, 7, 8], [(10, 12)], 5, 2)
    assert res["status"] == "infeasible" and res["counter"]["hits"] == 1
    # 告警远早于所有卡纸（提前量永远不够）-> 无法承诺
    res = solve([1, 2], [(100, 105)], 5, 1)
    assert res["status"] == "infeasible"
    # 告警提前量为 3/4/5 分钟 -> 最大可承诺 L=5，L=6 档反例配对 0 次
    res = solve([95, 96, 97], [(100, 100)], 10, 1)
    assert res["status"] == "ok" and res["L"] == 5
    assert res["counter"]["L"] == 6 and res["counter"]["hits"] == 0
    # 告警恰好都在窗口内 -> L 可取到 U
    res = solve([90], [(100, 100)], 10, 1)
    assert res["status"] == "ok" and res["L"] == 10 and "counter" not in res
    # U 大于可达提前量时被截断，且给出 L+1 反例
    res = solve([90], [(100, 100)], 50, 1)
    assert res["status"] == "ok" and res["L"] == 10
    assert res["counter"]["L"] == 11 and res["counter"]["hits"] == 0
    print("ok  边界情形")


IV_SAMPLE = ("125-130\n250-254\n370-374\n480-484\n600-604\n705-709")
PARAMS_OK = {"threshold": "80", "U": "90", "K": "5", "intervals": IV_SAMPLE}


def make_client():
    web.app.config.update(TESTING=True)
    c = web.app.test_client()
    assert c.get("/").status_code == 200
    assert len(c.get("/sample.csv").data.decode().strip().splitlines()) == 721
    r = c.post("/upload",
               data={"file": (_io.BytesIO(to_csv().encode()), "s.csv")},
               content_type="multipart/form-data", follow_redirects=True)
    assert "已加载" in r.get_data(as_text=True)
    return c


def get_store(c):
    with c.session_transaction() as sess:
        sid = sess["sid"]
    return web.STORE[sid]


def continue_payload(job):
    return {"job_id": job["id"], "rev": str(job["rev"])}


def drive_to_done(c, max_rounds=50):
    """从一次已提交的 /analyze 开始，按页面隐藏字段反复继续到出结论。"""
    store = get_store(c)
    job = store["job"]
    seen_budget = []
    while job["status"] != "done":
        before = job["calcs"]
        r = c.post("/continue", data=continue_payload(job),
                   follow_redirects=True)
        assert r.status_code == 200
        seen_budget.append(job["calcs"] - before)
        assert job["calcs"] - before <= 2, "单轮计算超过两次"
        assert len(seen_budget) <= max_rounds
    return job, seen_budget


def test_web_round_flow():
    """提交只跑第 1 轮（≤2 档），页面给双侧边界；继续到最终结论与原规则一致。"""
    c = make_client()

    dp_calls = {"n": 0}
    orig_dp = core._dp

    def counting_dp(*a, **k):
        dp_calls["n"] += 1
        return orig_dp(*a, **k)

    core._dp = counting_dp
    try:
        dp_calls["n"] = 0
        r = c.post("/analyze", data=PARAMS_OK, follow_redirects=True)
        first_calls = dp_calls["n"]
    finally:
        core._dp = orig_dp
    assert first_calls <= 2, first_calls

    html = r.get_data(as_text=True)
    store = get_store(c)
    job = store["job"]
    assert job["status"] == "running" and job["round_no"] == 1
    # 先核 L=1（命中 6），再二分中点 L=46（仍命中 6）=> 可承诺 46，坏端待核定
    assert job["best_ok"] == 46 and job["bad_min"] is None
    assert "④ 分轮核定进度" in html
    assert "已证实可承诺的最大档" in html and "不能承诺的最小档" in html
    assert "继续核定" in html and 'id="continue-form"' in html
    assert "L = 46 分钟" in html and "待核定" in html
    # 阶段结果不进历史
    assert store["history"] == [] and store["current"] is None

    job, budgets = drive_to_done(c)
    assert all(b <= 2 for b in budgets)
    html = c.get("/").get_data(as_text=True)
    assert "可承诺：L = 60 分钟" in html
    assert "任意卡纸时刻组合至少命中 5 次" in html
    assert "合计命中 4 次（共 6 次卡纸）" in html
    assert store["current"] is not None and job["status"] == "done"
    # 各档最坏命中只通过 min_hits 算过一次（反例走单独的 counterexample 求解）
    mh_levels = []
    orig_mh = core.min_hits

    def record_mh(L, *a, **k):
        mh_levels.append(L)
        return orig_mh(L, *a, **k)

    core.min_hits = record_mh
    try:
        # 已完成进度再发继续：直接判重，不产生任何 min_hits
        r = c.post("/continue", data=continue_payload(job))
        assert mh_levels == []
        assert r.status_code == 302
    finally:
        core.min_hits = orig_mh
    print("ok  分轮流程：首轮边界 46/待核定，逐轮收口到 L=60 + L=61 反例")


def test_round_budget_includes_counter():
    """监控 _dp：每次提交/继续的 DP 求解（含反例）至多 2 次。"""
    c = make_client()
    log = []
    orig_dp = core._dp

    def wrap(L, *a, **k):
        log.append(L)
        return orig_dp(L, *a, **k)

    core._dp = wrap
    try:
        c.post("/analyze", data=PARAMS_OK)
        assert len(log) <= 2, log
        store = get_store(c)
        job = store["job"]
        guard = 0
        while job["status"] != "done":
            log.clear()
            c.post("/continue", data=continue_payload(job))
            assert len(log) <= 2, ("单轮超过两次单档计算", log)
            guard += 1
            assert guard < 30
    finally:
        core._dp = orig_dp
    print("ok  每轮（含反例求解）DP 调用 ≤ 2")


def test_round_engine_matches_solve():
    """直接驱动 app.execute_round：500 组随机输入下，分轮结论与 core.solve 完全一致。"""
    rng = random.Random(424242)
    for trial in range(500):
        n = rng.randint(0, 12)
        alerts = sorted(rng.sample(range(0, 40), n))
        m = rng.randint(1, 5)
        U = rng.randint(1, 12)
        K = rng.randint(1, m + 2)
        intervals = []
        cur = rng.randint(0, 3)
        for _ in range(m):
            a = cur
            b = a + rng.randint(0, 3)
            intervals.append((a, b))
            cur = b + rng.randint(1, 4)

        job = {"id": "t", "rev": 0, "round_no": 0, "status": "running",
               "U": U, "K": K, "intervals": intervals, "alerts": alerts,
               "values": {}, "counters": {}, "calcs": 0, "round_calcs": 0,
               "calc_ms": 0.0, "pending_counter": None, "last_error": None,
               "params": {}, "data_name": "t", "data_rows": 0}
        store = {"seq": 1, "current": None}
        rounds = 0
        while job["status"] != "done":
            before = job["calcs"]
            web.execute_round(store, job)
            assert job["calcs"] - before <= 2
            job["rev"] += 1
            job["round_no"] += 1
            rounds += 1
            assert rounds < 100

        got = store["current"]["result"]
        ref = solve(alerts, intervals, U, K)
        assert got["status"] == ref["status"], trial
        if ref["status"] == "ok":
            assert got["L"] == ref["L"], trial
            assert got["min_hits"] == ref["min_hits"]
            if "counter" in ref:
                assert got["counter"]["L"] == ref["counter"]["L"]
                assert got["counter"]["hits"] == ref["counter"]["hits"]
                assert got["counter"]["combo"] == ref["counter"]["combo"]
            else:
                assert "counter" not in got
        else:
            assert got["counter"]["L"] == 1
            assert got["counter"]["hits"] == ref["counter"]["hits"]
    print("ok  分轮引擎 500 组随机结论与 core.solve 一致，每轮 ≤2 次")


def test_duplicate_continue_advances_once():
    """同一 rev 的继续表单重复提交，最多推进一轮。"""
    c = make_client()
    c.post("/analyze", data=PARAMS_OK)
    store = get_store(c)
    job = store["job"]
    rev0, round0 = job["rev"], job["round_no"]
    payload = continue_payload(job)

    r1 = c.post("/continue", data=payload, follow_redirects=True)
    rev1 = job["rev"]
    assert rev1 == rev0 + 1 and job["round_no"] == round0 + 1
    # 完全相同的请求再提两次（双击/刷新重提）
    c.post("/continue", data=payload, follow_redirects=True)
    c.post("/continue", data=payload, follow_redirects=True)
    assert job["rev"] == rev1, "重复提交又推进了进度"
    assert job["round_no"] == round0 + 1
    html = r1.get_data(as_text=True)
    assert "progress-panel" in html or "核定结论" in html

    # 不带 rev 的程序化调用：每次推进当前进度一轮（锁内串行）
    rev_before = job["rev"]
    c.post("/continue", data={"job_id": job["id"]})
    assert job["rev"] == rev_before + 1
    c.post("/continue", data={"job_id": job["id"]})
    assert job["rev"] == rev_before + 2
    print("ok  同一进度重复提交继续最多推进一轮（rev 判重）")


def test_failure_retains_tiers():
    """求解抛错时状态置 failed，已算档位保留；恢复后同 rev 重试不重算旧档。"""
    c = make_client()
    # K=1 时 L=1 可行，第 2 次调用（中点探测）令 min_hits 抛错
    c.post("/analyze", data={"threshold": "80", "U": "90", "K": "1",
                             "intervals": IV_SAMPLE})
    store = get_store(c)
    job = store["job"]
    assert job["status"] == "running" and "1" in job["values"]

    orig_mh = core.min_hits

    def boom(L, *a, **k):
        raise RuntimeError("simulated-failure")

    core.min_hits = boom
    try:
        rev_before = job["rev"]
        c.post("/continue", data=continue_payload(job),
               follow_redirects=True)
    finally:
        core.min_hits = orig_mh
    assert job["status"] == "failed"
    assert "simulated-failure" in job["last_error"]
    assert "1" in job["values"] and job["best_ok"] is not None
    assert job["rev"] == rev_before, "失败轮次不应消耗 rev"
    html = c.get("/").get_data(as_text=True)
    assert "求解失败" in html and "已算完" in html

    seen = []

    def counting(L, alerts, intervals, U):
        seen.append(L)
        return orig_mh(L, alerts, intervals, U)

    core.min_hits = counting
    try:
        c.post("/continue", data=continue_payload(job),
               follow_redirects=True)
    finally:
        core.min_hits = orig_mh
    assert 1 not in seen, "已算完的 L=1 被重算"
    assert job["status"] in ("running", "done")
    print("ok  求解失败保留已算档位，重试不重算")


def test_invalidation_and_withdraw():
    """校验失败不动旧状态；新核定/换 CSV/清空成功后旧结论与旧进度作废。"""
    c = make_client()
    c.post("/analyze", data=PARAMS_OK)
    store = get_store(c)
    job = store["job"]
    old_jid, old_rev = job["id"], job["rev"]
    c.post("/continue", data=continue_payload(job))
    assert job["rev"] == old_rev + 1

    # 校验失败（区间重叠）：旧进度原样保留
    r = c.post("/analyze", data={"threshold": "80", "U": "90", "K": "5",
                                 "intervals": "125-130\n128-135"},
               follow_redirects=True)
    assert "重叠" in r.get_data(as_text=True)
    assert store["job"] is job and job["rev"] == old_rev + 1

    # 用旧 rev 重复继续：只推进一轮（校验失败没有改变 rev）
    payload_old = continue_payload(job)
    c.post("/continue", data=payload_old)
    assert job["rev"] == old_rev + 2
    c.post("/continue", data=payload_old)
    assert job["rev"] == old_rev + 2

    # 新核定通过：旧的进行中进度作废（不进历史），产生新 job
    hist_len = len(store["history"])
    c.post("/analyze", data={"threshold": "80", "U": "90", "K": "5",
                             "intervals": "126-130\n250-254\n370-374\n"
                                          "480-484\n600-604\n705-709"})
    new_job = store["job"]
    assert new_job["id"] != old_jid
    assert len(store["history"]) == hist_len, "未完成的阶段结果不应进历史"
    # 旧表单的继续请求被拒，不影响新进度
    rev_new = new_job["rev"]
    r = c.post("/continue", data={"job_id": old_jid, "rev": "99"},
               follow_redirects=True)
    assert "旧核定进度" in r.get_data(as_text=True)
    assert new_job["rev"] == rev_new

    # 把第一个核定做到出结论，再更正：旧结论撤下并进历史
    drive_to_done(c)
    assert store["current"] is not None
    c.post("/analyze", data=PARAMS_OK)
    html = c.get("/").get_data(as_text=True)
    assert "已撤下" in html and "参数或卡纸区间已更正" in html
    assert len(store["history"]) >= 1

    # 核定途中换 CSV：成功上传后进度/结论作废
    c.post("/continue", data=continue_payload(store["job"]))
    assert store["job"]["status"] == "running"
    r = c.post("/upload",
               data={"file": (_io.BytesIO(to_csv().encode()), "other.csv")},
               content_type="multipart/form-data", follow_redirects=True)
    assert "原核定进度与结论已作废" in r.get_data(as_text=True)
    assert store["job"] is None and store["current"] is None
    r = c.post("/continue", data={"job_id": old_jid, "rev": "1"},
               follow_redirects=True)
    assert "已作废" in r.get_data(as_text=True)

    # 清空也作废
    c.post("/analyze", data=PARAMS_OK)
    assert store["job"] is not None
    c.post("/reset", follow_redirects=True)
    assert store["data"] is None and store["job"] is None
    print("ok  校验/新核定/换 CSV/清空的作废与撤下语义")


def test_web_infeasible_and_validation():
    """K 超过卡纸数时首次提交的第 2 次计算即反例，当轮给出无法承诺。"""
    c = make_client()
    r = c.post("/analyze", data={"threshold": "80", "U": "90", "K": "7",
                                 "intervals": IV_SAMPLE},
               follow_redirects=True)
    html = r.get_data(as_text=True)
    assert "无法承诺" in html
    assert "下例仅命中 6 次" in html
    store = get_store(c)
    assert store["current"]["result"]["status"] == "infeasible"

    # 非法 CSV（分钟不连续）被拦截，且不作废已有结论
    r = c.post("/upload",
               data={"file": (_io.BytesIO(b"minute,score\n1,50\n3,60\n"),
                              "b.csv")},
               content_type="multipart/form-data", follow_redirects=True)
    assert "分钟号不连续" in r.get_data(as_text=True)
    assert get_store(c)["current"] is not None

    # 非法参数被拦截
    r = c.post("/analyze", data={"threshold": "80", "U": "0", "K": "5",
                                 "intervals": "1-2"},
               follow_redirects=True)
    assert "≥ 1" in r.get_data(as_text=True)
    print("ok  无法承诺当轮收口 + 校验拦截保留旧结论")


def test_web_app():
    """保留的原始网页冒烟路径（分轮后继续到结论，再走更正撤下）。"""
    c = make_client()
    r = c.post("/analyze", data=PARAMS_OK, follow_redirects=True)
    assert "分轮核定进度" in r.get_data(as_text=True)
    drive_to_done(c)
    html = c.get("/").get_data(as_text=True)
    assert "可承诺：L = 60 分钟" in html
    assert "任意卡纸时刻组合至少命中 5 次" in html
    assert "合计命中 4 次（共 6 次卡纸）" in html

    # 更正区间：旧结论撤下并出现在历史
    r = c.post("/analyze", data={
        "threshold": "80", "U": "90", "K": "5",
        "intervals": "126-130\n250-254\n370-374\n480-484\n600-604\n705-709"},
        follow_redirects=True)
    html = r.get_data(as_text=True)
    assert "已撤下" in html and "参数或卡纸区间已更正" in html

    # K=7 超过卡纸数：无法承诺（首次提交内完成）
    r = c.post("/analyze", data={"threshold": "80", "U": "90", "K": "7",
                                 "intervals": IV_SAMPLE},
               follow_redirects=True)
    assert "无法承诺" in r.get_data(as_text=True)

    # 非法输入被拦截
    r = c.post("/upload",
               data={"file": (_io.BytesIO(b"minute,score\n1,50\n3,60\n"),
                              "b.csv")},
               content_type="multipart/form-data", follow_redirects=True)
    assert "分钟号不连续" in r.get_data(as_text=True)
    r = c.post("/analyze", data={"threshold": "80", "U": "90", "K": "5",
                                 "intervals": "125-130\n128-135"},
               follow_redirects=True)
    assert "重叠" in r.get_data(as_text=True)
    print("ok  网页端冒烟测试（含分轮继续）")


def main():
    test_compute_alerts()
    rng = random.Random(20261124)
    test_max_matching_vs_brute(rng)
    test_min_hits_vs_brute(rng)
    test_solve_vs_linear(rng)
    test_monotonicity(rng)
    test_edge_cases()
    test_web_round_flow()
    test_round_budget_includes_counter()
    test_round_engine_matches_solve()
    test_duplicate_continue_advances_once()
    test_failure_retains_tiers()
    test_invalidation_and_withdraw()
    test_web_infeasible_and_validation()
    test_web_app()
    print("\n全部自测通过。")


if __name__ == "__main__":
    main()
