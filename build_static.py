"""research.json(뉴스·일정·수수료·ETF 구성) + 직접 편집하는 thesis/plan/journal -> data/ 데이터셋."""
import json, re, shutil
from pathlib import Path

ROOT = Path(__file__).parent
OUT = ROOT / "data"
r = json.loads((ROOT / "research.json").read_text())
f = r["fees"]

news = [{k: n.get(k, "") for k in ("ticker", "date", "headline_ko", "summary_ko", "impact", "impact_reason_ko", "source", "url")} for n in r["news"]]
calendar = [{"date": c["date"], "event_ko": c["event_ko"], "why_ko": c["why_ko"], "related": c.get("related", "")}
            for c in r["calendar"] if re.match(r"^\d{4}-\d{2}-\d{2}$", c["date"])]

costs = [
    {"key": "kr_commission_pct", "label": "국내 매매 수수료", "value": f["toss_domestic_commission_pct"], "unit": "%", "note": "매수·매도 각각. 유관기관 제비용(약 0.0036%)은 별도. 토스증권 공식 공지로는 미확인."},
    {"key": "us_commission_pct", "label": "해외 매매 수수료", "value": f["toss_overseas_commission_pct"], "unit": "%", "note": "미국 주식 매수·매도 각각. 공식 공지로는 미확인."},
    {"key": "fx_spread_pct", "label": "환전 비용", "value": 0.05, "unit": "%", "note": "평일 09:00~15:30 환전 기준(95% 우대). 그 밖의 시간·주말은 0.5%로 10배. 원화와 달러 사이를 오갈 때만 붙습니다. 블로그 정리 기준, 공식 미확인."},
    {"key": "us_sec_fee_pct", "label": "미국 SEC 수수료", "value": 0.00206, "unit": "%", "note": "미국 주식 매도 때만. 2026년 4월 4일부터 적용된 요율."},
    {"key": "kr_stock_tax_pct", "label": "국내 개별주식 거래세", "value": f.get("kr_kospi_total_with_rural_tax_pct", 0.2), "unit": "%", "note": "개별 주식 매도 때만. 국내 상장 ETF는 면제."},
    {"key": "etf_dividend_tax_pct", "label": "기타 ETF(국내주식형 외) 매매차익 과세", "value": 15.4, "unit": "%", "note": "PLUS 글로벌HBM반도체 같은 ETF. 매매차익과 과표기준가 증가분 중 작은 금액에 15.4% 원천징수, ETF 안의 국내 주식 차익은 과세에서 빠져 실제 세금은 시뮬레이터 값보다 작을 수 있습니다. 손실은 다른 ETF 이익과 상계 불가. 분배금도 15.4%."},
    {"key": "kr_equity_etf_note", "label": "국내주식형 ETF", "value": "", "unit": "", "note": "TIGER 반도체TOP10은 매매차익 비과세. 분배금만 15.4%."},
    {"key": "us_cgt_pct", "label": "해외주식 양도소득세", "value": 22, "unit": "%", "note": "1년 해외 손익 합계에서 기본공제 후. 다음 해 5월 직접 신고·납부."},
    {"key": "us_cgt_deduction_krw", "label": "해외주식 기본공제", "value": 2500000, "unit": "원", "note": "1년 기준."},
]

group = {"SK하이닉스": "SK하이닉스 계열", "SK스퀘어": "SK하이닉스 계열", "삼성전자": "삼성전자", "Micron Technology": "마이크론"}
etf_rows = []
for code, label in (("396500.KS", "TIGER 반도체TOP10"), ("442580.KS", "PLUS 글로벌HBM반도체")):
    for h in r["etf_holdings"].get(code, [])[:7]:
        name = re.sub(r"\s*\(.*?\)\s*", "", h["name"]).strip()
        if not name or "판독" in h["name"]:
            continue
        etf_rows.append({"etf_ticker": code, "etf": label, "holding": name, "group": group.get(name, name), "weight_pct": h["weight_pct"],
                         "note": "SK하이닉스 지분이 자산의 대부분인 지주사. 주가가 하이닉스와 함께 움직입니다." if name == "SK스퀘어" else ""})

data = {"news": news, "calendar": calendar, "costs": costs, "etf_holdings": etf_rows}
for k, v in data.items():
    (OUT / f"{k}.json").write_text(json.dumps(v, ensure_ascii=False, separators=(",", ":")))
for k in ("thesis", "plan", "journal"):
    shutil.copy(ROOT / f"{k}.json", OUT / f"{k}.json")
(OUT / "rates.json").unlink(missing_ok=True)
print({k: len(v) for k, v in data.items()})
