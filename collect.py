"""매일 아침 실행: 가격·지표를 받아 대시보드용 JSON 데이터셋을 data/ 에 쓴다."""
import json, math, os, sys, time, urllib.parse, urllib.request, datetime as dt
from pathlib import Path

ROOT = Path(__file__).parent
OUT = ROOT / "data"
UA = {"User-Agent": "Mozilla/5.0"}
SIM_AMOUNT = 500_000  # 후보 표에서 쓰는 기준 추가금(원)
QUARTER = 13  # 3개월 = 13주


def fetch_chart(ticker, rng="1y"):
    """일별 종가. 분배금이 반영된 수정종가(adjclose)가 있으면 그것을 쓴다."""
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(ticker)}?range={rng}&interval=1d"
    err = None
    for _ in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=20) as r:
                res = json.load(r)["chart"]["result"][0]
            ts = res["timestamp"]
            close = res["indicators"]["quote"][0]["close"]
            adj = (res["indicators"].get("adjclose") or [{}])[0].get("adjclose") or close
            tz = res["meta"].get("gmtoffset", 0)
            day = lambda t: dt.datetime.fromtimestamp(t + tz, dt.timezone.utc).date().isoformat()
            reg = (res["meta"].get("currentTradingPeriod") or {}).get("regular") or {}
            live = ticker != "KRW=X" and not ticker.endswith("=F") and reg.get("start", 0) <= time.time() < reg.get("end", 0)
            keep = [i for i, t in enumerate(ts) if not (live and t >= reg["start"])]  # 장이 아직 안 끝난 오늘 봉은 버림
            rows = [(day(ts[i]), close[i]) for i in keep if close[i] is not None]
            adj_rows = [(day(ts[i]), adj[i]) for i in keep if adj[i] is not None]
            return {"meta": res["meta"], "rows": rows, "adj": adj_rows, "dropped_live": live}
        except Exception as e:  # noqa: BLE001
            err = e
            time.sleep(2)
    raise RuntimeError(f"{ticker}: {err}")


def fred_last(series_id):
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=20) as r:
        lines = [l for l in r.read().decode().strip().splitlines()[1:] if "," in l and not l.endswith(",.") and not l.endswith(",")]
    d, v = lines[-1].split(",")
    cutoff = (dt.date.fromisoformat(d) - dt.timedelta(days=30)).isoformat()
    d0, v0 = next((l.split(",") for l in reversed(lines) if l.split(",")[0] <= cutoff), lines[0].split(","))
    return d, float(v), float(v0)


def weekly(rows):
    """일별 종가 -> ISO 주 마지막 종가. 한국·미국 장 시간 차이를 줄이려고 주간 수익률을 쓴다."""
    out = {}
    for d, c in rows:
        y, w, _ = dt.date.fromisoformat(d).isocalendar()
        out[(y, w)] = c
    return out


def price_stats(rows):
    """현지 통화 종가 기준 추세 지표(차트 앱에서 보는 숫자와 같게)."""
    closes = [c for _, c in rows]
    ma = lambda n: sum(closes[-n:]) / min(n, len(closes))
    return {"vs_ma200": closes[-1] / ma(200) - 1, "vs_ma50": closes[-1] / ma(50) - 1, "from_high": closes[-1] / max(closes) - 1}


def main():
    # 실제 보유 내역은 저장소에 올리지 않고 환경변수(GitHub Secret)로 넣을 수 있다
    pos_cfg = json.loads(os.environ["POSITIONS_JSON"]) if os.environ.get("POSITIONS_JSON") else json.loads((ROOT / "positions.json").read_text())
    uni = json.loads((ROOT / "universe.json").read_text())
    rules = json.loads((ROOT / "rules.json").read_text())
    research = json.loads((ROOT / "research.json").read_text()) if (ROOT / "research.json").exists() else {}
    positions, candidates, market = pos_cfg["positions"], uni["candidates"], uni["market"]

    tickers = {p["ticker"] for p in positions} | {c["ticker"] for c in candidates} | {m["ticker"] for m in market}
    charts, failed = {}, []
    for t in sorted(tickers):
        try:
            charts[t] = fetch_chart(t)
        except Exception as e:  # noqa: BLE001
            failed.append(str(e))
    if "KRW=X" not in charts:
        sys.exit("환율을 받지 못해 중단: " + "; ".join(failed))

    fx_rows = charts["KRW=X"]["rows"]
    fx_dates = [d for d, _ in fx_rows]
    fx_now, fx_prev = fx_rows[-1][1], fx_rows[-2][1]

    def fx_on(d):
        """그날(없으면 직전 영업일) 원/달러."""
        lo, hi = 0, len(fx_dates) - 1
        if d < fx_dates[0]:
            return fx_rows[0][1]
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if fx_dates[mid] <= d:
                lo = mid
            else:
                hi = mid - 1
        return fx_rows[lo][1]

    def krw_rows(t, currency):
        """원화 기준 수정종가. 달러 자산은 같은 날 환율로 환산해 환율 변동까지 넣는다."""
        rows = charts[t]["adj"]
        return rows if currency == "KRW" else [(d, c * fx_on(d)) for d, c in rows]

    meta = {}
    for p in positions:
        meta[p["ticker"]] = {"name": p["name"], "currency": p["currency"], "kind": "보유", "category": p["theme"],
                             "exposure": p["theme"], "semis": p["semis_lookthrough"], "etf_tax": p.get("etf_tax", "us_stock")}
    for c in candidates:
        meta[c["ticker"]] = {"name": c["name"], "currency": "USD" if c["market"] == "US" else "KRW", "kind": "후보",
                             "category": c["category"], "exposure": c["exposure"], "semis": c["semis_lookthrough"],
                             "etf_tax": c.get("etf_tax", "us_stock")}
    assets = [t for t in meta if t in charts]

    # ---------- 주간 원화 수익률과 공분산(연율) ----------
    wk = {t: weekly(krw_rows(t, meta[t]["currency"])) for t in assets}
    common = sorted(set.intersection(*(set(v) for v in wk.values())))[:-1]  # 진행 중인 이번 주 제외
    rets = {t: [wk[t][common[i]] / wk[t][common[i - 1]] - 1 for i in range(1, len(common))] for t in assets}
    n = len(common) - 1
    mean = {t: sum(rets[t]) / n for t in assets}
    C = {(a, b): sum((x - mean[a]) * (y - mean[b]) for x, y in zip(rets[a], rets[b])) / (n - 1) * 52 for a in assets for b in assets}

    def worst_quarter(series):
        """주간 수익률로 이은 최근 1년 중 가장 나빴던 13주 누적 수익률."""
        level = [1.0]
        for r in series:
            level.append(level[-1] * (1 + r))
        return min(level[i + QUARTER] / level[i] - 1 for i in range(len(level) - QUARTER))

    # ---------- 보유 종목 ----------
    holdings = []
    for p in positions:
        t = p["ticker"]
        if t not in charts:
            continue
        rows = charts[t]["rows"]
        price, prev = rows[-1][1], rows[-2][1]
        usd = p["currency"] == "USD"
        cost_fx = pos_cfg["cost_fx_overseas"] if usd else 1
        value = p["qty"] * price * (fx_now if usd else 1)
        value_prev = p["qty"] * prev * (fx_on(rows[-2][0]) if usd else 1)
        cost = p["qty"] * p["avg_cost"] * cost_fx
        pnl_price = p["qty"] * (price - p["avg_cost"]) * cost_fx if usd else value - cost
        holdings.append({
            "ticker": t, "name": p["name"], "theme": p["theme"], "currency": p["currency"], "semis": p["semis_lookthrough"],
            "qty": p["qty"], "avg_cost": p["avg_cost"], "price": price, "price_date": rows[-1][0],
            "day_chg": price / prev - 1, "day_pnl_krw": round(value - value_prev),
            "value_krw": round(value), "cost_krw": round(cost), "pnl_krw": round(value - cost),
            "pnl_price_krw": round(pnl_price), "pnl_fx_krw": round(value - cost - pnl_price),
            "pnl_pct": value / cost - 1, "breakeven_pct": cost / value - 1,
            "vol_1y": math.sqrt(C[(t, t)]), "worst_13w": worst_quarter(rets[t]),
            "etf_tax": p.get("etf_tax", "us_stock"), **price_stats(rows),
        })
    total = sum(h["value_krw"] for h in holdings)
    total_cost = sum(h["cost_krw"] for h in holdings)
    w0 = {h["ticker"]: h["value_krw"] for h in holdings}

    def port_vol(values):
        tot = sum(values.values())
        w = {k: v / tot for k, v in values.items() if k != "cash"}
        return math.sqrt(max(sum(w[a] * w[b] * C[(a, b)] for a in w for b in w), 0))

    vol_now = port_vol(w0)
    port_ret = [sum(w0[t] / total * rets[t][i] for t in w0) for i in range(n)]
    for h in holdings:
        w = h["value_krw"] / total
        h["weight"] = w
        h["loss_contrib"] = h["pnl_krw"] / total_cost
        h["risk_share"] = w * sum(w0[k] / total * C[(h["ticker"], k)] for k in w0) / vol_now ** 2
    day_pnl = sum(h["day_pnl_krw"] for h in holdings)

    themes = {}
    for h in holdings:
        themes[h["theme"]] = themes.get(h["theme"], 0) + h["value_krw"]
    theme_rows = sorted(({"theme": k, "value_krw": v, "weight": v / total} for k, v in themes.items()), key=lambda r: -r["weight"])
    semis_val = sum(h["value_krw"] * h["semis"] for h in holdings)

    # ---------- 후보 비교: 현금 기준선 대비 ----------
    def corr(a, b):
        ma, mb = sum(a) / len(a), sum(b) / len(b)
        cov = sum((x - ma) * (y - mb) for x, y in zip(a, b))
        va, vb = sum((x - ma) ** 2 for x in a), sum((y - mb) ** 2 for y in b)
        return cov / math.sqrt(va * vb)

    cash_vol = port_vol({**w0, "cash": SIM_AMOUNT})
    cash_delta = cash_vol - vol_now

    # 현금 대비 차이(diversif_delta)의 표본오차: 4주 블록 부트스트랩
    import random
    rng = random.Random(7)
    BLOCK, REPS = 4, 400
    boot = {t: [] for t in assets}
    for _ in range(REPS):
        idx = []
        while len(idx) < n:
            s0 = rng.randrange(0, n - BLOCK + 1)
            idx.extend(range(s0, s0 + BLOCK))
        idx = idx[:n]
        sample = {t: [rets[t][i] for i in idx] for t in assets}
        mu = {t: sum(sample[t]) / n for t in assets}
        cb = {}
        def cv(a, b):
            k = (a, b) if a <= b else (b, a)
            if k not in cb:
                cb[k] = sum((x - mu[a]) * (y - mu[b]) for x, y in zip(sample[a], sample[b])) / (n - 1) * 52
            return cb[k]
        def pv(values):
            tot = sum(values.values())
            w = {k: v / tot for k, v in values.items() if k != "cash"}
            return math.sqrt(max(sum(w[a] * w[b] * cv(a, b) for a in w for b in w), 0))
        v0 = pv(w0)
        cd = pv({**w0, "cash": SIM_AMOUNT}) - v0
        for t in assets:
            boot[t].append(pv({**w0, t: w0.get(t, 0) + SIM_AMOUNT}) - v0 - cd)
    se = {t: math.sqrt(sum((x - sum(b) / REPS) ** 2 for x in b) / (REPS - 1)) for t, b in boot.items()}
    screen = [{
        "ticker": "CASH", "name": "현금으로 두기", "kind": "기준", "category": "기준선", "exposure": "현금", "market": "KR",
        "currency": "KRW", "price": 1, "etf_tax": "none", "semis": 0, "corr_port": None, "corr_note": "",
        "vol_1y": 0, "ret_1y": None, "worst_13w": 0, "port_vol_after": cash_vol, "port_vol_delta": cash_delta,
        "dilution_delta": cash_delta, "diversif_delta": 0, "diversif_se": 0, "semis_after": semis_val / (total + SIM_AMOUNT), "band": "기준",
    }]
    for t in assets:
        m = meta[t]
        held = t in w0
        if held:  # 자기 자신이 섞이지 않도록 '이 종목을 뺀 나머지 계좌'와의 상관
            rest = {k: v for k, v in w0.items() if k != t}
            rt = sum(rest.values())
            other = [sum(rest[k] / rt * rets[k][i] for k in rest) for i in range(n)]
        else:
            other = port_ret
        after = port_vol({**w0, t: w0.get(t, 0) + SIM_AMOUNT})
        delta = after - vol_now
        div = delta - cash_delta
        adj = charts[t]["adj"]
        screen.append({
            "ticker": t, "name": m["name"], "kind": m["kind"], "category": m["category"], "exposure": m["exposure"],
            "market": "US" if m["currency"] == "USD" else "KR", "currency": m["currency"], "price": charts[t]["rows"][-1][1],
            "etf_tax": m["etf_tax"], "semis": m["semis"],
            "corr_port": corr(rets[t], other), "corr_note": "이 종목을 뺀 계좌와의 상관" if held else "",
            "vol_1y": math.sqrt(C[(t, t)]), "ret_1y": adj[-1][1] / adj[0][1] - 1, "worst_13w": worst_quarter(rets[t]),
            "port_vol_after": after, "port_vol_delta": delta, "dilution_delta": cash_delta, "diversif_delta": div,
            "semis_after": (semis_val + SIM_AMOUNT * m["semis"]) / (total + SIM_AMOUNT),
            "diversif_se": se[t],
            "band": "현금보다 더 줄임" if div < -2 * se[t] else "현금과 비슷" if div <= 2 * se[t] else "현금보다 덜 줄임" if delta < 0 else "오히려 늘림",
        })
    order = {"기준": 0, "현금보다 더 줄임": 1, "현금과 비슷": 2, "현금보다 덜 줄임": 3, "오히려 늘림": 4}
    screen.sort(key=lambda r: (order[r["band"]], r["exposure"], r["market"]))

    # ---------- 시장 지표 ----------
    mkt = []
    for m in market:
        if m["ticker"] not in charts:
            continue
        rows = charts[m["ticker"]]["rows"]
        v, v1, v22 = rows[-1][1], rows[-2][1], rows[-22][1]
        row = {"ticker": m["ticker"], "label": m["label"], "unit": m["unit"], "why": m["why"], "value": v, "date": rows[-1][0],
               "chg_1d": v / v1 - 1, "chg_1m": v / v22 - 1, "chg_1d_bp": None, "chg_1m_bp": None, "note": ""}
        if m["unit"] == "%":
            row["chg_1d_bp"], row["chg_1m_bp"] = (v - v1) * 100, (v - v22) * 100
        if m["ticker"] == "KRW=X":
            row["note"] = "수집 시각의 해외 시세 · 서울 외환시장 종가나 뉴스 수치와 다를 수 있음"
        mkt.append(row)
    rates = research.get("rates", {})
    try:
        d, v, v0 = fred_last("DFF")
        mkt.append({"ticker": "DFF", "label": "미 기준금리", "unit": "%", "why": "한국과 미국의 금리 차이는 원/달러 환율에 영향을 줍니다.",
                    "value": v, "date": d, "chg_1d": None, "chg_1m": None, "chg_1d_bp": None, "chg_1m_bp": (v - v0) * 100,
                    "note": f"실효금리 · 목표 {rates.get('fed_funds_target_range', '')} · 다음 결정 {str(rates.get('fomc_next_decision', '2026-12-09'))[:10]}"})
    except Exception as e:  # noqa: BLE001
        failed.append(f"DFF: {e}")
    if rates.get("bok_base_rate_pct") is not None:
        mkt.append({"ticker": "BOK", "label": "한국 기준금리", "unit": "%", "why": "올리면 원화 강세로 달러 자산의 원화 평가액이 줄 수 있습니다.",
                    "value": rates["bok_base_rate_pct"], "date": str(rates.get("bok_last_decision_date", ""))[:10],
                    "chg_1d": None, "chg_1m": None, "chg_1d_bp": None, "chg_1m_bp": None,
                    "note": f"다음 결정 {str(rates.get('bok_next_meeting', ''))[:10]} · 미국 목표 상단과의 차이 {round((float(str(rates.get('fed_funds_target_range', '0')).replace('%', '').split('–')[-1]) - rates['bok_base_rate_pct']) * 100)}bp"})
    try:
        d, v, v0 = fred_last("BAMLH0A0HYM2")
        mkt.append({"ticker": "HYOAS", "label": "미 하이일드 스프레드", "unit": "%", "why": "넓어지면 회사채 발행이 줄어 S&P 글로벌 신용평가 수수료에 부담이 됩니다.",
                    "value": v, "date": d, "chg_1d": None, "chg_1m": None, "chg_1d_bp": None, "chg_1m_bp": (v - v0) * 100, "note": ""})
    except Exception as e:  # noqa: BLE001
        failed.append(f"HY spread: {e}")

    series = [{"ticker": t, "date": d, "close": round(c, 4)}
              for t in [h["ticker"] for h in holdings] + [m["ticker"] for m in market if m["ticker"] in charts]
              for d, c in charts[t]["rows"][-90:]]

    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    port_worst = worst_quarter(port_ret)
    kospi = next((r for r in mkt if r["ticker"] == "^KS11"), {})
    sox = next((r for r in mkt if r["ticker"] == "^SOX"), {})
    summary = [{
        "asof": now, "total_krw": total, "cost_krw": total_cost, "pnl_krw": total - total_cost,
        "pnl_pct": total / total_cost - 1, "day_pnl_krw": day_pnl, "day_pnl_pct": day_pnl / (total - day_pnl),
        "fx": fx_now, "fx_prev": fx_prev, "cost_fx_overseas": pos_cfg["cost_fx_overseas"],
        "semis_weight": semis_val / total, "semis_label_weight": themes.get("반도체", 0) / total,
        "port_vol": vol_now, "port_worst_13w": port_worst, "weeks_used": n, "sim_amount": SIM_AMOUNT,
        "kospi_day": kospi.get("chg_1d"), "sox_day": sox.get("chg_1d"),
        "bok_rate": rates.get("bok_base_rate_pct"), "us_close_date": charts["^GSPC"]["rows"][-1][0] if "^GSPC" in charts else None,
        "kr_close_date": charts["^KS11"]["rows"][-1][0] if "^KS11" in charts else None,
        "research_asof": research.get("asof"), "failed": "; ".join(failed),
    }]

    files = {"summary": summary, "holdings": holdings, "themes": theme_rows, "market": mkt, "series": series,
             "screen": screen, "cov": [{"a": a, "b": b, "cov": C[(a, b)]} for a in assets for b in assets], "rules": rules}
    OUT.mkdir(exist_ok=True)
    for k, v in files.items():
        (OUT / f"{k}.json").write_text(json.dumps(v, ensure_ascii=False, separators=(",", ":")))
    print(json.dumps({k: len(v) for k, v in files.items()}), "failed:", failed or "none")


if __name__ == "__main__":
    main()
