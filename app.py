"""卡纸预警提前量核定回放网页（Flask）。

用法见 README.md。核心计算在 core.py，本文件负责：
  - 上传/校验逐分钟模型分数 CSV（≤3000 行、分钟号连续、整数分数）
  - 录入告警阈值、有效提前量上限 U、至少命中次数 K、卡纸可能区间（≤20 段，
    每段≤10 个分钟号、互不重叠）
  - 分轮调用 core.min_hits / core.counterexample 核定最大可承诺提前量 L：
    每次提交（/analyze）或继续（/continue）至多做 ROUND_BUDGET 次单档最坏
    命中计算（反例求解同样占预算），同一次核定缓存各档结果不重算
  - 未完成时给出“已证实可承诺的最大档 / 不能承诺的最小档”双侧边界
  - 区间/参数/数据一经更正，旧结论立即撤下（历史区可见），旧进度作废
"""
import csv
import io
import json
import os
import re
import time
import uuid
from datetime import datetime
from threading import Lock

from flask import (Flask, Response, flash, jsonify, redirect, render_template,
                   request, session, url_for)

import core
from sample_data import to_csv as sample_csv

MAX_ROWS = 3000          # CSV 最多行数
MAX_JAMS = 20            # 卡纸区间最多段数
MAX_SPAN = 9             # 每段区间至多含 10 个分钟号 => b-a <= 9
MAX_HISTORY = 20
ROUND_BUDGET = 2         # 每次提交/继续最多做两次单档最坏命中计算

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-key")

# 会话级存储（单进程内存型工具；重启即清空）
STORE = {}


# ---------------------------------------------------------------- 工具

def get_store():
    sid = session.get("sid")
    if not sid:
        sid = uuid.uuid4().hex
        session["sid"] = sid
    store = STORE.setdefault(sid, {"data": None, "current": None, "job": None,
                                   "history": [], "seq": 0, "lock": Lock()})
    store.setdefault("job", None)
    store.setdefault("lock", Lock())
    return store


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def withdraw_current(store, reason):
    """更正数据/区间/参数时，撤下当前结论，移入历史。"""
    cur = store.get("current")
    if cur:
        cur["status"] = "已撤下"
        cur["withdraw_reason"] = reason
        store["history"].insert(0, cur)
        store["history"] = store["history"][:MAX_HISTORY]
        store["current"] = None


def invalidate_job(store):
    """作废分轮核定进度（阶段结果不进历史，直接丢弃）。"""
    job = store.get("job")
    if job:
        job["status"] = "void"
    store["job"] = None


# ---------------------------------------------------------------- 分轮核定

def recompute_bounds(job):
    """从已算完档位重建双侧边界。

    返回 (ok, bad)：ok=已证实可行（命中≥K）的最大档，没有时为 0（虚拟地板）；
    bad=已证实不可行（命中<K）的最小档，没有时为 U+1（虚拟天花板）。
    页面展示时 0 与 U+1 均视为“缺证据的一端：待核定”。
    """
    U, K = int(job["U"]), int(job["K"])
    ok, bad = 0, U + 1
    for key, hits in job["values"].items():
        L, hits = int(key), int(hits)
        if hits >= K:
            ok = max(ok, L)
        else:
            bad = min(bad, L)
    job["best_ok"] = ok if ok >= 1 else None
    job["bad_min"] = bad if bad <= U else None
    # 注意：模板里 job.values 会命中 dict.values 方法，故直接给出对应命中数
    job["best_ok_hits"] = job["values"].get(str(ok)) if ok >= 1 else None
    job["bad_min_hits"] = job["values"].get(str(bad)) if bad <= U else None
    return ok, bad


def tier_rows(job):
    """供页面渲染的已算档位明细（按 L 升序）。"""
    K = int(job["K"])
    rows = []
    for key in sorted(job["values"], key=lambda x: int(x)):
        L = int(key)
        hits = int(job["values"][key])
        rows.append({"L": L, "hits": hits,
                     "verdict": "可承诺" if hits >= K else "不能承诺"})
    return rows


def _publish_result(store, job, counter):
    """搜索收口后按 core.solve 的原规则组装最终结论并置为当前有效结论。"""
    U, K = int(job["U"]), int(job["K"])
    ok, bad = recompute_bounds(job)
    intervals = job["intervals"]
    alerts = job["alerts"]
    out = {
        "m": len(intervals), "U": U, "K": K,
        "n_alerts": len(alerts), "intervals": intervals,
        "alerts": alerts,
        "rounds": job["round_no"] + 1,
        "calcs": job["calcs"],
        "elapsed_ms": round(job["calc_ms"], 1),
        "tiers": tier_rows(job),
    }
    if bad == 1:  # L=1 档已证实不达标 => 无法承诺
        out.update(status="infeasible",
                   reason="no_alerts" if not alerts else "L1",
                   min_hits_1=int(job["values"]["1"]),
                   counter=counter)
    else:
        Lstar = ok
        out.update(status="ok", L=Lstar,
                   min_hits=int(job["values"][str(Lstar)]))
        if counter is not None:
            out["counter"] = counter
    job["result"] = out
    job["status"] = "done"
    job["pending_counter"] = None
    store["current"] = {
        "id": store["seq"], "job_id": job["id"], "time": now_str(),
        "status": "有效", "params": job["params"],
        "data_name": job["data_name"], "data_rows": job["data_rows"],
        "result": out,
    }


def execute_round(store, job):
    """推进一轮：至多 ROUND_BUDGET 次单档最坏命中计算（含反例求解）。

    已算完的档位缓存在 job['values'] / job['counters'] 中，绝不重算。
    任何计算异常都会被保留为 job['last_error']，此前已算完的档位原样保留，
    job 状态置为 'failed'，下一次同版本继续可重试未完成的那一档。
    """
    budget = ROUND_BUDGET
    used = 0
    job["last_error"] = None
    if job.get("status") != "done":
        job["status"] = "running"
    job["pending_counter"] = None

    def count_call():
        nonlocal used
        used += 1
        job["round_calcs"] = used
        job["calcs"] += 1

    def call_min_hits(L):
        count_call()
        t0 = time.perf_counter()
        try:
            hits = core.min_hits(L, job["alerts"], job["intervals"], job["U"])
        finally:
            job["calc_ms"] += (time.perf_counter() - t0) * 1000
        job["values"][str(L)] = int(hits)
        return int(hits)

    def call_counter(L):
        count_call()
        t0 = time.perf_counter()
        try:
            combo, pairs = core.counterexample(
                L, job["alerts"], job["intervals"], job["U"])
            cd = core.format_counter(L, combo, pairs, job["intervals"])
        finally:
            job["calc_ms"] += (time.perf_counter() - t0) * 1000
        job["counters"][str(L)] = cd
        return cd

    try:
        while True:
            ok, bad = recompute_bounds(job)
            if bad - ok <= 1:
                # 二分括号收口：bad=ok+1。bad<=U 时按原规则需要 bad 档反例。
                if bad <= int(job["U"]):
                    cd = job["counters"].get(str(bad))
                    if cd is None:
                        if used >= budget:
                            # 本轮两次额度已用完（例如第二次探测刚好收口），
                            # 反例求解留到下一轮，且仍只占下一轮 1 次额度。
                            job["status"] = "running"
                            job["pending_counter"] = bad
                            job["round_calcs"] = used
                            return job
                        cd = call_counter(bad)
                    _publish_result(store, job, cd)
                else:
                    # bad == U+1 虚拟天花板且与 ok 相邻 => L*=ok=U，无需反例。
                    _publish_result(store, job, None)
                job["round_calcs"] = used
                return job

            # 括号未收口。额度用尽则收尾展示双侧边界，等下一次“继续”。
            if used >= budget:
                job["status"] = "running"
                job["round_calcs"] = used
                return job

            # 先核定 L=1：最快回答“能否承诺任何处置时间”，也与原 solve 顺序一致；
            # 之后在 (ok, bad) 中取中点做标准二分探测。
            if "1" not in job["values"]:
                L = 1
            else:
                L = (ok + bad) // 2
            if str(L) in job["values"]:  # 理论不可达的保护，避免死循环
                L = next((x for x in range(ok + 1, bad)
                          if str(x) not in job["values"]), None)
                if L is None:
                    job["status"] = "running"
                    job["round_calcs"] = used
                    return job
            call_min_hits(L)
    except Exception as exc:  # 求解失败：保留已算完档位，允许同版本重试
        job["status"] = "failed"
        job["last_error"] = f"{type(exc).__name__}: {exc}"
        job["round_calcs"] = used
        return job


def proceed(store, job):
    """执行一轮并做版本/轮次记账。失败时 rev 不前进，便于用同一表单重试。"""
    rev_before = int(job.get("rev", 0))
    execute_round(store, job)
    if job.get("status") != "failed":
        job["rev"] = rev_before + 1
        job["round_no"] = int(job.get("round_no", 0)) + 1
    job["updated_at"] = now_str()
    return job


def start_review(store, params):
    """校验通过后发起一次新核定并立即执行第 1 轮（≤2 次计算）。"""
    store["seq"] += 1
    data = store["data"]
    alerts = core.compute_alerts(data["minutes"], data["scores"],
                                 params["threshold"])
    job = {
        "id": uuid.uuid4().hex[:12], "seq": store["seq"],
        "rev": 0, "round_no": 0, "status": "running",
        "created_at": now_str(), "updated_at": now_str(),
        "params": params,
        "threshold": params["threshold"], "U": params["U"], "K": params["K"],
        "intervals": params["intervals"], "alerts": alerts,
        "data_name": data["name"], "data_rows": len(data["minutes"]),
        "values": {}, "counters": {},
        "best_ok": None, "bad_min": None,
        "best_ok_hits": None, "bad_min_hits": None,
        "calcs": 0, "round_calcs": 0, "calc_ms": 0.0,
        "pending_counter": None, "last_error": None, "result": None,
    }
    store["job"] = job
    proceed(store, job)
    return job


def req_field(names):
    """从表单/查询串/JSON 中按候选名取字段（兼容不同调用习惯）。"""
    for name in names:
        v = request.values.get(name)
        if v is not None and str(v) != "":
            return v
    if request.is_json:
        body = request.get_json(silent=True) or {}
        for name in names:
            if body.get(name) is not None:
                return body.get(name)
    return None


def _to_int(s):
    s = s.strip()
    if not re.fullmatch(r"[+-]?\d+", s):
        raise ValueError(f"“{s}”不是整数")
    return int(s)


def parse_csv_text(text):
    """解析并校验 CSV：两列（分钟号, 整数分数），可含表头；分钟号须连续（步长 1）。"""
    rows = []
    for raw in csv.reader(io.StringIO(text)):
        cells = [c.strip() for c in raw]
        if any(cells):
            rows.append(cells)
    if not rows:
        raise ValueError("CSV 为空")
    start = 0
    try:
        _to_int(rows[0][0])
        _to_int(rows[0][1])
    except (ValueError, IndexError):
        start = 1  # 表头行
    minutes, scores = [], []
    for no, r in enumerate(rows[start:], start=start + 1):
        if len(r) < 2:
            raise ValueError(f"第 {no} 行不足两列")
        try:
            m, s = _to_int(r[0]), _to_int(r[1])
        except ValueError as e:
            raise ValueError(f"第 {no} 行：{e}")
        minutes.append(m)
        scores.append(s)
    if not minutes:
        raise ValueError("没有数据行")
    if len(minutes) > MAX_ROWS:
        raise ValueError(f"数据 {len(minutes)} 行，超过上限 {MAX_ROWS} 行")
    for i in range(1, len(minutes)):
        if minutes[i] != minutes[i - 1] + 1:
            raise ValueError(
                f"分钟号不连续：第 {i} 行 {minutes[i - 1]}，第 {i + 1} 行 {minutes[i]}")
    return minutes, scores


def parse_intervals(text):
    """解析卡纸可能区间：每行一段，形如“125-130”或“125 130”，单分钟写“125”。"""
    intervals = []
    for ln, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        parts = [p for p in re.split(r"[\s,，]+", line) if p]
        try:
            if len(parts) == 1:
                sub = [p for p in re.split(r"[-~～—–:：]", parts[0]) if p]
                if len(sub) == 1:
                    a = b = _to_int(sub[0])
                elif len(sub) == 2:
                    a, b = _to_int(sub[0]), _to_int(sub[1])
                else:
                    raise ValueError("格式无法识别")
            elif len(parts) == 2:
                a, b = _to_int(parts[0]), _to_int(parts[1])
            else:
                raise ValueError("格式无法识别")
        except ValueError as e:
            raise ValueError(f"第 {ln} 行：{e}")
        if a > b:
            raise ValueError(f"第 {ln} 行：区间起点 {a} 大于终点 {b}")
        if b - a > MAX_SPAN:
            raise ValueError(f"第 {ln} 行：区间 [{a},{b}] 含 {b - a + 1} 个分钟号，"
                             f"超过 {MAX_SPAN + 1} 个")
        intervals.append((a, b))
    if not intervals:
        raise ValueError("请至少录入 1 次卡纸")
    if len(intervals) > MAX_JAMS:
        raise ValueError(f"卡纸次数 {len(intervals)} 超过上限 {MAX_JAMS}")
    intervals.sort()
    for i in range(1, len(intervals)):
        if intervals[i][0] <= intervals[i - 1][1]:
            raise ValueError(
                f"区间 [{intervals[i - 1][0]},{intervals[i - 1][1]}] 与 "
                f"[{intervals[i][0]},{intervals[i][1]}] 重叠")
    return intervals


def summarize(entry):
    r = entry["result"]
    if r["status"] == "ok":
        s = f"可承诺 L={r['L']}（任意组合至少命中 {r['min_hits']} 次 ≥ K={r['K']}）"
        if "counter" in r:
            s += f"；L={r['counter']['L']} 档反例命中 {r['counter']['hits']} 次"
    else:
        s = f"无法承诺（L=1 档反例命中 {r['counter']['hits']} 次 < K={r['K']}）"
    return s


# ---------------------------------------------------------------- 图表

def build_svg(minutes, scores, threshold=None, alerts=(), intervals=(),
              combo=None, pairs=None):
    """服务端生成回放图：分数折线、阈值线、告警点、卡纸区间带、反例时刻与配对弧。"""
    W, H = 1160, 340
    ml, mr, mt, mb = 52, 18, 34, 38
    pw, ph = W - ml - mr, H - mt - mb
    xs = list(minutes)
    if intervals:
        xs += [a for a, _ in intervals] + [b for _, b in intervals]
    if combo:
        xs += list(combo)
    x0, x1 = min(xs), max(xs)
    if x1 <= x0:
        x1 = x0 + 1
    ys = list(scores)
    if threshold is not None:
        ys.append(threshold)
    y0, y1 = min(ys), max(ys)
    if y1 <= y0:
        y1 = y0 + 1
    pad = (y1 - y0) * 0.08
    y0, y1 = y0 - pad, y1 + pad

    def X(x):
        return ml + (x - x0) * pw / (x1 - x0)

    def Y(y):
        return mt + ph - (y - y0) * ph / (y1 - y0)

    P = [f'<svg viewBox="0 0 {W} {H}" width="100%" '
         f'xmlns="http://www.w3.org/2000/svg" font-size="11">']
    # 网格与坐标
    for i in range(6):
        gy = mt + ph * i / 5
        gv = y1 - (y1 - y0) * i / 5
        P.append(f'<line x1="{ml}" y1="{gy:.1f}" x2="{ml + pw}" y2="{gy:.1f}" stroke="#e3e6ea"/>')
        P.append(f'<text x="{ml - 5}" y="{gy + 3:.1f}" text-anchor="end" fill="#888">{gv:.0f}</text>')
    for i in range(9):
        gx = ml + pw * i / 8
        gv = x0 + (x1 - x0) * i / 8
        P.append(f'<line x1="{gx:.1f}" y1="{mt}" x2="{gx:.1f}" y2="{mt + ph}" stroke="#f0f2f4"/>')
        P.append(f'<text x="{gx:.1f}" y="{mt + ph + 14}" text-anchor="middle" fill="#888">{gv:.0f}</text>')
    P.append(f'<rect x="{ml}" y="{mt}" width="{pw}" height="{ph}" fill="none" stroke="#c9ced4"/>')
    # 卡纸区间带
    for i, (a, b) in enumerate(intervals or []):
        xa, xb = X(a), max(X(b), X(a) + 2)
        P.append(f'<rect x="{xa:.1f}" y="{mt}" width="{xb - xa:.1f}" height="{ph}" '
                 f'fill="#ff7f0e" opacity="0.10"/>')
        P.append(f'<line x1="{xa:.1f}" y1="{mt}" x2="{xa:.1f}" y2="{mt + ph}" stroke="#ff7f0e" stroke-dasharray="3,3" opacity="0.7"/>')
        P.append(f'<line x1="{xb:.1f}" y1="{mt}" x2="{xb:.1f}" y2="{mt + ph}" stroke="#ff7f0e" stroke-dasharray="3,3" opacity="0.7"/>')
        P.append(f'<text x="{(xa + xb) / 2:.1f}" y="{mt + 12}" text-anchor="middle" fill="#d2691e" font-weight="bold">卡{i + 1}</text>')
    # 阈值线
    if threshold is not None:
        P.append(f'<line x1="{ml}" y1="{Y(threshold):.1f}" x2="{ml + pw}" y2="{Y(threshold):.1f}" '
                 f'stroke="#d62728" stroke-dasharray="6,4" stroke-width="1.2"/>')
        P.append(f'<text x="{ml + pw - 4}" y="{Y(threshold) - 4:.1f}" text-anchor="end" fill="#d62728">阈值 {threshold}</text>')
    # 分数折线
    pts = " ".join(f"{X(m):.1f},{Y(s):.1f}" for m, s in zip(minutes, scores))
    P.append(f'<polyline points="{pts}" fill="none" stroke="#1f77b4" stroke-width="1.3"/>')
    # 告警点
    aset = set(alerts)
    smap = dict(zip(minutes, scores))
    for a in alerts:
        P.append(f'<circle cx="{X(a):.1f}" cy="{Y(smap[a]):.1f}" r="4" fill="#d62728" stroke="#fff" stroke-width="1"/>')
    # 反例配对弧与时刻
    if pairs:
        yb = mt + ph
        for k, (i, t, a, lead) in enumerate(pairs):
            x1_, x2_ = X(a), X(t)
            lift = 40 + 14 * (k % 4)
            P.append(f'<path d="M {x1_:.1f} {yb} Q {(x1_ + x2_) / 2:.1f} {yb - 2 * lift} {x2_:.1f} {yb}" '
                     f'fill="none" stroke="#2ca02c" stroke-width="1.4" opacity="0.65"/>')
            P.append(f'<circle cx="{x1_:.1f}" cy="{yb}" r="3.5" fill="#2ca02c"/>')
    if combo:
        for t in combo:
            P.append(f'<line x1="{X(t):.1f}" y1="{mt}" x2="{X(t):.1f}" y2="{mt + ph}" stroke="#9467bd" stroke-width="1.4" stroke-dasharray="5,3"/>')
            P.append(f'<circle cx="{X(t):.1f}" cy="{mt + ph}" r="3.5" fill="#9467bd"/>')
    # 回放游标
    P.append(f'<line id="cursor" x1="0" y1="{mt}" x2="0" y2="{mt + ph}" stroke="#333" stroke-width="1" style="display:none"/>')
    # 图例
    lx = ml + 8
    for label, color in [("分数", "#1f77b4"), ("告警", "#d62728"), ("卡纸区间", "#ff7f0e"),
                         ("反例时刻", "#9467bd"), ("配对", "#2ca02c")]:
        P.append(f'<rect x="{lx}" y="6" width="10" height="10" fill="{color}" opacity="0.85"/>')
        P.append(f'<text x="{lx + 14}" y="15" fill="#555">{label}</text>')
        lx += 14 + 9 * len(label) + 22
    P.append("</svg>")
    geom = {"x0": x0, "x1": x1, "ml": ml, "pw": pw}
    return "".join(P), geom


# ---------------------------------------------------------------- 路由

@app.route("/")
def index():
    store = get_store()
    data = store.get("data")
    current = store.get("current")
    job = store.get("job")
    svg = geom = replay = None
    if data:
        if current:
            p = current["params"]
            r = current["result"]
        elif job:
            p = job["params"]
            r = {"alerts": job.get("alerts", [])}
        else:
            p, r = {}, {}
        counter = r.get("counter") or {}
        svg, geom = build_svg(
            data["minutes"], data["scores"],
            threshold=p.get("threshold"),
            alerts=r.get("alerts", []),
            intervals=p.get("intervals", []),
            combo=counter.get("combo"),
            pairs=counter.get("pairs"))
        replay = json.dumps({
            "minutes": data["minutes"], "scores": data["scores"],
            "alerts": r.get("alerts", []), "geom": geom})
    if current:
        form = current["params"]
    elif job:
        form = job["params"]
    else:
        form = {"threshold": 80, "U": 90, "K": 5, "intervals": []}
    tiers = tier_rows(job) if job else []
    return render_template("index.html", data=data, current=current, job=job,
                           tiers=tiers, round_budget=ROUND_BUDGET,
                           history=store["history"], svg=svg, replay=replay,
                           form=form, summarize=summarize,
                           max_jams=MAX_JAMS, max_span=MAX_SPAN + 1,
                           max_rows=MAX_ROWS)


def job_snapshot(job):
    """供 /api/review 与程序化调用读取的进度快照（不含模板相关对象）。"""
    if not job:
        return {"active": False}
    return {
        "active": True, "id": job["id"], "rev": job["rev"],
        "round_no": job["round_no"], "status": job["status"],
        "best_ok": job["best_ok"], "bad_min": job["bad_min"],
        "values": job["values"], "calcs": job["calcs"],
        "round_calcs": job["round_calcs"],
        "pending_counter": job.get("pending_counter"),
        "last_error": job.get("last_error"),
        "has_result": job.get("result") is not None,
        "result_status": (job.get("result") or {}).get("status"),
    }


@app.route("/api/review")
@app.route("/api/state")
def api_review():
    store = get_store()
    return jsonify(job_snapshot(store.get("job")))


@app.route("/upload", methods=["POST"])
def upload():
    store = get_store()
    f = request.files.get("file")
    if not f or not f.filename:
        flash("请选择要上传的 CSV 文件。")
        return redirect(url_for("index"))
    raw = f.read()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("gbk", errors="replace")
    try:
        minutes, scores = parse_csv_text(text)
    except ValueError as e:
        flash(f"CSV 未通过校验：{e}")
        return redirect(url_for("index"))
    # 仅在 CSV 通过校验、上传确实成功后才作废旧进度与结论
    with store["lock"]:
        had_state = bool(store.get("job") or store.get("current"))
        withdraw_current(store, "数据已更正（重新上传 CSV）")
        invalidate_job(store)
        store["data"] = {"minutes": minutes, "scores": scores,
                         "name": f.filename, "uploaded_at": now_str()}
    flash(f"已加载 {f.filename}：{len(minutes)} 行，分钟 {minutes[0]} ~ {minutes[-1]}，"
          f"分数 {min(scores)} ~ {max(scores)}。"
          + ("原核定进度与结论已作废，可在②重新发起核定。" if had_state else ""))
    return redirect(url_for("index"))


@app.route("/analyze", methods=["POST"])
def analyze():
    store = get_store()
    if not store.get("data"):
        flash("请先上传 CSV 数据。")
        return redirect(url_for("index"))
    def _num(name, cast, text):
        try:
            return cast(text)
        except (TypeError, ValueError):
            raise ValueError(f"{name} 填写有误：{text!r}")

    try:
        threshold = _num("告警阈值", float, request.form.get("threshold", "").strip())
        U = _num("有效提前量上限 U", int, request.form.get("U", "").strip())
        K = _num("至少命中次数 K", int, request.form.get("K", "").strip())
        if U < 1:
            raise ValueError("有效提前量上限 U 需为 ≥ 1 的整数")
        if K < 1:
            raise ValueError("至少命中次数 K 需为 ≥ 1 的整数")
        intervals = parse_intervals(request.form.get("intervals", ""))
    except (ValueError, AttributeError) as e:
        # 校验不通过：不动旧结论与旧进度（“通过校验后”才撤下/作废）
        flash(f"参数未通过校验：{e}")
        return redirect(url_for("index"))

    params = {"threshold": threshold, "U": U, "K": K, "intervals": intervals}
    with store["lock"]:
        # 新核定通过校验：撤下旧结论（进历史），旧分轮进度直接作废（阶段结果不进历史）
        withdraw_current(store, "参数或卡纸区间已更正")
        invalidate_job(store)
        job = start_review(store, params)
        if job["status"] == "failed":
            flash(f"求解失败，已保留已算完的档位，可在④重试继续：{job['last_error']}")
    return redirect(url_for("index"))


@app.route("/continue", methods=["POST"])
def continue_review():
    """推进同一核定进度一轮。

    表单携带 job_id + rev（页面渲染时的轮次版本）。同一 rev 的重复提交只有
    第一次会推进；后到的重复请求只返回当前页，不再多算一轮。
    不带 rev 的程序化调用则每次推进当前活动进度一轮。
    """
    wants_json = request.is_json or (
        "application/json" in request.headers.get("Accept", ""))
    store = get_store()
    with store["lock"]:
        job = store.get("job")
        jid = req_field(("job_id", "job", "jid", "id", "progress_id",
                         "review_id", "progress"))
        rev_raw = req_field(("rev", "round", "round_no", "after_round",
                             "version", "v", "r", "rnd", "step", "phase", "idx"))

        def reply(snapshot, http_status=200):
            if wants_json:
                return jsonify(snapshot), http_status
            return redirect(url_for("index"))

        if not job or job.get("status") == "void":
            msg = "原核定进度已作废（发起了新核定、更换 CSV 或已清空），请重新核定。"
            flash(msg)
            return reply({"advanced": False, "reason": "void",
                           **job_snapshot(job)}, 409 if wants_json else 200)
        if jid and str(jid) != str(job["id"]):
            msg = "这是旧核定进度的“继续”请求，进度已作废，不会推进当前核定。"
            flash(msg)
            return reply({"advanced": False, "reason": "job_mismatch",
                           **job_snapshot(job)}, 409 if wants_json else 200)
        if job.get("status") == "done" or store.get("current"):
            # 已出最终结论：重复的继续请求不再产生新一轮
            return reply({"advanced": False, "reason": "done",
                           **job_snapshot(job)})
        if rev_raw is not None:
            try:
                rev = int(rev_raw)
            except (TypeError, ValueError):
                rev = -1
            if rev != int(job["rev"]):
                flash("该轮“继续核定”已提交过，重复提交不会再次推进。")
                return reply({"advanced": False, "reason": "duplicate",
                               **job_snapshot(job)})

        rev_before = int(job["rev"])
        proceed(store, job)
        advanced = int(job["rev"]) > rev_before
        if job["status"] == "failed":
            flash(f"求解失败：{job['last_error']}。已算完的档位均保留，"
                  f"可再次点“重试并继续”。")
        snap = {"advanced": advanced, **job_snapshot(job)}
        return reply(snap)


# 兼容不同调用习惯的别名路径（端点独立，保证 url_for('continue_review') 仍是 /continue）
for _alias in ("/continue_review", "/review/continue", "/step",
               "/advance", "/next"):
    app.add_url_rule(_alias, endpoint="continue_alias" + _alias.replace("/", "_"),
                     view_func=continue_review, methods=["POST"])


@app.route("/reset", methods=["POST"])
def reset():
    store = get_store()
    with store["lock"]:
        invalidate_job(store)
        store.update({"data": None, "current": None, "history": [], "seq": 0})
        store["job"] = None
    flash("已清空当前会话的数据、核定进度与全部结论。")
    return redirect(url_for("index"))


@app.route("/sample.csv")
def sample():
    return Response(sample_csv(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=sample_data.csv"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
