"""
港股情绪看板 - 数据抓取脚本

指标：
1. 南向资金净流入（东方财富 datacenter-web API，具备完整历史数据，可一次性回填）
2. 大市成交额（AAStocks 大巿沽空页，逐日快照累积历史）
3. 沽空比率（AAStocks 大巿沽空页，逐日快照累积历史）

用法：
    python scripts/fetch_data.py

会读取/写入仓库根目录下的 market_data.json。
南向资金每次运行都会重新拉取近 N 天完整历史并覆盖对应区间（避免遗漏调整）；
成交额与沽空比率因为只有当日快照可用，采用"读取旧数据 -> 追加/更新今天 -> 写回"的方式逐日累积。
"""
from __future__ import annotations

import json
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_FILE = ROOT / "market_data.json"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}

HKT = timezone(timedelta(hours=8))

# 南向资金历史回填天数（东方财富接口按日降序返回，取到这个天数之前的数据即可停止翻页）
SOUTHBOUND_BACKFILL_DAYS = 1100  # 约 3 年


def http_get(url: str, params: dict | None = None, timeout: int = 20) -> str:
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# 1. 南向资金
# ---------------------------------------------------------------------------

def fetch_southbound_history() -> list[dict]:
    """从东方财富 datacenter-web API 拉取南向资金历史净流入数据。

    reportName=RPT_MUTUAL_DEAL_HISTORY, filter MUTUAL_TYPE="006" 对应南向资金
    （北向 005 / 沪股通 001 / 深股通 003 / 南向 006 / 港股通沪 002 / 港股通深 004）。
    NET_DEAL_AMT 原始单位需除以 100 转换为"亿元"，与 akshare 的 stock_hsgt_hist_em 实现一致。
    """
    cutoff = datetime.now(HKT).date() - timedelta(days=SOUTHBOUND_BACKFILL_DAYS)
    all_rows: list[dict] = []
    page = 1
    page_size = 200

    while True:
        params = {
            "sortColumns": "TRADE_DATE",
            "sortTypes": "-1",
            "pageSize": str(page_size),
            "pageNumber": str(page),
            "reportName": "RPT_MUTUAL_DEAL_HISTORY",
            "columns": "ALL",
            "source": "WEB",
            "client": "WEB",
            "filter": '(MUTUAL_TYPE="006")',
        }
        raw = http_get("https://datacenter-web.eastmoney.com/api/data/v1/get", params=params)
        payload = json.loads(raw)
        result = payload.get("result") or {}
        rows = result.get("data") or []
        if not rows:
            break
        all_rows.extend(rows)

        oldest_date_str = rows[-1].get("TRADE_DATE", "")[:10]
        try:
            oldest_date = datetime.strptime(oldest_date_str, "%Y-%m-%d").date()
        except ValueError:
            oldest_date = None

        total_pages = result.get("pages", page)
        if page >= total_pages or (oldest_date and oldest_date < cutoff):
            break
        page += 1
        time.sleep(0.25)

    seen = {}
    for r in all_rows:
        date = (r.get("TRADE_DATE") or "")[:10]
        net = r.get("NET_DEAL_AMT")
        if not date or net is None:
            continue
        seen[date] = {
            "date": date,
            "net_flow": round(net / 100, 2),  # 亿港元
        }

    records = sorted(seen.values(), key=lambda x: x["date"])
    records = [r for r in records if r["date"] >= cutoff.isoformat()]
    return records


# ---------------------------------------------------------------------------
# 2 & 3. 大市成交额 + 沽空比率（同一页面）
# ---------------------------------------------------------------------------

def _strip_tags(html: str) -> str:
    html = re.sub(r"<script[\s\S]*?</script>", " ", html, flags=re.I)
    html = re.sub(r"<style[\s\S]*?</style>", " ", html, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text


def fetch_aastocks_snapshot() -> dict | None:
    """抓取 AAStocks 大巿沽空页当日快照：沽空比率（当日/前一日）、沽空金额、成交金额。"""
    url = "https://www.aastocks.com/tc/stocks/market/shortselling/securities-eligible.aspx"
    raw = http_get(url)
    text = _strip_tags(raw)

    m_ratio = re.search(r"前比率[:：]\s*([\d.]+)%\s*([\d.]+)%", text)
    m_short_amt = re.search(r"沽空金額\s*([\d,]+\.?\d*)億", text)
    m_turnover = re.search(r"成交金額\s*([\d,]+\.?\d*)億", text)
    m_asof = re.search(r"沽空資料截至\s*(\d{4})/(\d{2})/(\d{2})\s*(\d{2}:\d{2}:\d{2})", text)

    if not (m_ratio and m_short_amt and m_turnover):
        return None

    prev_ratio = float(m_ratio.group(1))
    curr_ratio = float(m_ratio.group(2))
    short_amount = float(m_short_amt.group(1).replace(",", ""))
    turnover = float(m_turnover.group(1).replace(",", ""))

    if m_asof:
        y, mo, d, hms = m_asof.groups()
        data_date = f"{y}-{mo}-{d}"
    else:
        data_date = datetime.now(HKT).date().isoformat()

    return {
        "date": data_date,
        "short_ratio": curr_ratio,
        "prev_short_ratio": prev_ratio,
        "short_amount": short_amount,  # 亿港元
        "turnover": turnover,  # 亿港元（沽空研究覆盖的可沽空证券总成交额，作为大巿成交额的近似）
    }


# ---------------------------------------------------------------------------
# 数据合并与落盘
# ---------------------------------------------------------------------------

def load_existing() -> dict:
    if DATA_FILE.exists():
        try:
            return json.loads(DATA_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {"southbound": [], "turnover": [], "short_ratio": []}


def merge_by_date(existing: list[dict], new: list[dict]) -> list[dict]:
    by_date = {r["date"]: r for r in existing}
    for r in new:
        by_date[r["date"]] = r
    return sorted(by_date.values(), key=lambda x: x["date"])


def main() -> None:
    data = load_existing()

    errors = []

    try:
        southbound = fetch_southbound_history()
        if southbound:
            data["southbound"] = merge_by_date(data.get("southbound", []), southbound)
            print(f"南向资金: 获取 {len(southbound)} 条记录")
        else:
            errors.append("southbound: 未获取到数据")
    except Exception as e:
        errors.append(f"southbound: {e}")
        print(f"南向资金抓取失败: {e}")

    try:
        snapshot = fetch_aastocks_snapshot()
        if snapshot:
            data["turnover"] = merge_by_date(
                data.get("turnover", []),
                [{"date": snapshot["date"], "turnover": snapshot["turnover"]}],
            )
            data["short_ratio"] = merge_by_date(
                data.get("short_ratio", []),
                [{
                    "date": snapshot["date"],
                    "short_ratio": snapshot["short_ratio"],
                    "prev_short_ratio": snapshot["prev_short_ratio"],
                    "short_amount": snapshot["short_amount"],
                }],
            )
            print(f"大市成交额/沽空比率快照: {snapshot}")
        else:
            errors.append("aastocks snapshot: 未能解析页面")
    except Exception as e:
        errors.append(f"aastocks snapshot: {e}")
        print(f"AAStocks 快照抓取失败: {e}")

    data["last_updated"] = datetime.now(HKT).strftime("%Y-%m-%d %H:%M:%S")
    if errors:
        data["_errors"] = errors
    else:
        data.pop("_errors", None)

    DATA_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"已写入 {DATA_FILE}")


if __name__ == "__main__":
    main()
