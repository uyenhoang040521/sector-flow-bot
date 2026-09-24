"""
Sector Flow Bot — báo cáo dòng tiền crypto theo sector mỗi 24h.

Nguồn dữ liệu (miễn phí):
  - CoinGecko  /coins/categories            → market cap, % thay đổi 24h, volume 24h theo category
  - DefiLlama  stablecoins.llama.fi         → nguồn cung stablecoin theo từng chain (hiện tại / 1 ngày / 1 tuần trước)

Đầu ra:
  - reports/latest.md, reports/YYYY-MM-DD.md   (báo cáo markdown tiếng Việt)
  - data/history/YYYY-MM-DD.json               (snapshot để tính đột biến volume so với TB 7 ngày)
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

# ───────────────────────────── Cấu hình ─────────────────────────────
ROOT = Path(__file__).resolve().parent
HISTORY_DIR = ROOT / "data" / "history"
REPORT_DIR = ROOT / "reports"

CONFIG = {
    "min_category_mcap": float(os.getenv("MIN_CATEGORY_MCAP", 500_000_000)),   # bỏ category < $500M
    "min_category_volume": float(os.getenv("MIN_CATEGORY_VOLUME", 5_000_000)), # bỏ category volume < $5M
    "min_chain_stable": float(os.getenv("MIN_CHAIN_STABLE", 10_000_000)),      # bỏ chain có < $10M stablecoin
    "exclude_keywords": ["portfolio", "holdings", "launchpool", "launchpad", "alleged"],
    "top_n": int(os.getenv("TOP_N", 10)),
    "volume_spike_min_ratio": 1.5,   # volume hôm nay >= 1.5x TB 7 ngày thì coi là đột biến
    "volume_spike_min_days": 3,      # cần ít nhất 3 ngày lịch sử mới tính đột biến
    "history_keep_days": 45,
}

COINGECKO_URL = "https://api.coingecko.com/api/v3/coins/categories"
STABLECOINS_URL = "https://stablecoins.llama.fi/stablecoins?includePrices=true"
VN_TZ = timezone(timedelta(hours=7))


# ───────────────────────────── Fetch ─────────────────────────────
def http_get_json(url: str, headers: dict | None = None, retries: int = 4):
    last_err = None
    for attempt in range(retries):
        try:
            r = requests.get(url, headers=headers or {}, timeout=60)
            if r.status_code == 429:  # rate limit → chờ rồi thử lại
                time.sleep(15 * (attempt + 1))
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            last_err = e
            time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"Không lấy được {url}: {last_err}")


def fetch_categories() -> list[dict]:
    headers = {"accept": "application/json"}
    key = os.getenv("COINGECKO_API_KEY")
    if key:  # Demo key miễn phí giúp ổn định rate limit
        headers["x-cg-demo-api-key"] = key
    return http_get_json(COINGECKO_URL, headers)


def fetch_stablecoins() -> dict:
    return http_get_json(STABLECOINS_URL)


# ───────────────────────────── Phân tích sector ─────────────────────────────
def _num(x) -> float | None:
    try:
        return float(x) if x is not None else None
    except (TypeError, ValueError):
        return None


def clean_categories(raw: list[dict]) -> list[dict]:
    out = []
    for c in raw:
        mc, pct, vol = _num(c.get("market_cap")), _num(c.get("market_cap_change_24h")), _num(c.get("volume_24h"))
        name = (c.get("name") or "").strip()
        if mc is None or pct is None or vol is None or not name:
            continue
        if mc < CONFIG["min_category_mcap"] or vol < CONFIG["min_category_volume"]:
            continue
        if any(k in name.lower() for k in CONFIG["exclude_keywords"]):
            continue
        if pct <= -100:
            continue
        prev_mc = mc / (1 + pct / 100)
        out.append({
            "id": c.get("id"),
            "name": name,
            "is_ecosystem": "ecosystem" in name.lower(),
            "mcap": mc,
            "mcap_pct_24h": pct,
            "mcap_delta_usd": mc - prev_mc,
            "volume_24h": vol,
            "turnover": vol / mc if mc else 0,
            "top_coins": c.get("top_3_coins_id") or [],
        })
    return out


def load_history(today: str) -> dict[str, dict]:
    hist = {}
    if not HISTORY_DIR.exists():
        return hist
    for f in sorted(HISTORY_DIR.glob("*.json")):
        if f.stem < today:
            try:
                hist[f.stem] = json.loads(f.read_text())
            except json.JSONDecodeError:
                pass
    return hist


def add_volume_spikes(cats: list[dict], history: dict[str, dict]) -> None:
    last7 = [history[d] for d in sorted(history)[-7:]]
    for c in cats:
        vols = [snap["categories"][c["id"]]["volume_24h"]
                for snap in last7 if c["id"] in snap.get("categories", {})]
        if len(vols) >= CONFIG["volume_spike_min_days"]:
            avg = sum(vols) / len(vols)
            c["vol_avg_7d"] = avg
            c["vol_ratio"] = c["volume_24h"] / avg if avg else None
        else:
            c["vol_avg_7d"] = None
            c["vol_ratio"] = None


# ───────────────────────────── Phân tích stablecoin ─────────────────────────────
def stablecoin_flows(data: dict) -> tuple[list[dict], dict]:
    chains: dict[str, dict] = {}
    total = {"current": 0.0, "prev_day": 0.0, "prev_week": 0.0}

    for asset in data.get("peggedAssets", []):
        if asset.get("pegType") != "peggedUSD" or asset.get("deadFrom"):
            continue
        for chain, v in (asset.get("chainCirculating") or {}).items():
            cur = _num((v.get("current") or {}).get("peggedUSD"))
            if cur is None:
                continue
            d = _num((v.get("circulatingPrevDay") or {}).get("peggedUSD"))
            w = _num((v.get("circulatingPrevWeek") or {}).get("peggedUSD"))
            row = chains.setdefault(chain, {"chain": chain, "current": 0.0,
                                            "cur_d": 0.0, "prev_day": 0.0,
                                            "cur_w": 0.0, "prev_week": 0.0})
            row["current"] += cur
            # Chỉ cộng vào phép so sánh khi có đủ dữ liệu kỳ trước (tránh thổi phồng do thiếu data)
            if d is not None:
                row["cur_d"] += cur
                row["prev_day"] += d
            if w is not None:
                row["cur_w"] += cur
                row["prev_week"] += w

        c = _num((asset.get("circulating") or {}).get("peggedUSD"))
        d = _num((asset.get("circulatingPrevDay") or {}).get("peggedUSD"))
        w = _num((asset.get("circulatingPrevWeek") or {}).get("peggedUSD"))
        if c is not None and d is not None and w is not None:
            total["current"] += c
            total["prev_day"] += d
            total["prev_week"] += w

    out = []
    for r in chains.values():
        if r["current"] < CONFIG["min_chain_stable"]:
            continue
        r["delta_24h"] = r["cur_d"] - r["prev_day"]
        r["delta_7d"] = r["cur_w"] - r["prev_week"]
        r["pct_24h"] = r["delta_24h"] / r["prev_day"] * 100 if r["prev_day"] else None
        r["pct_7d"] = r["delta_7d"] / r["prev_week"] * 100 if r["prev_week"] else None
        out.append(r)
    return out, total


# ───────────────────────────── Format ─────────────────────────────
def usd(x: float, signed: bool = False) -> str:
    sign = ("+" if x >= 0 else "−") if signed else ("−" if x < 0 else "")
    a = abs(x)
    for unit, div in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if a >= div:
            return f"{sign}${a / div:,.2f}{unit}"
    return f"{sign}${a:,.0f}"


def pct(x: float | None) -> str:
    return "—" if x is None else f"{x:+.2f}%"


def md_table(headers: list[str], rows: list[list[str]]) -> str:
    if not rows:
        return "_Không có dữ liệu._\n"
    lines = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    lines += ["| " + " | ".join(r) + " |" for r in rows]
    return "\n".join(lines) + "\n"


def build_report(cats: list[dict], chains: list[dict], total: dict, now: datetime, history_days: int) -> str:
    n = CONFIG["top_n"]
    sectors = [c for c in cats if not c["is_ecosystem"]]
    ecos = [c for c in cats if c["is_ecosystem"]]

    def cat_row(i, c):
        coins = ", ".join(c["top_coins"][:3])
        return [str(i), c["name"], pct(c["mcap_pct_24h"]), usd(c["mcap_delta_usd"], True),
                usd(c["mcap"]), usd(c["volume_24h"]), coins]

    cat_headers = ["#", "Sector", "MC 24h", "Δ MC", "Market cap", "Volume 24h", "Top coins"]

    top_sectors = sorted([c for c in sectors if c["mcap_pct_24h"] > 0], key=lambda c: c["mcap_pct_24h"], reverse=True)[:n]
    worst_sectors = sorted([c for c in sectors if c["mcap_pct_24h"] < 0], key=lambda c: c["mcap_pct_24h"])[:5]
    top_ecos = sorted([c for c in ecos if c["mcap_pct_24h"] > 0], key=lambda c: c["mcap_pct_24h"], reverse=True)[:5]
    top_abs = sorted([c for c in sectors if c["mcap_delta_usd"] > 0], key=lambda c: c["mcap_delta_usd"], reverse=True)[:5]

    spikes = [c for c in cats if c.get("vol_ratio") and c["vol_ratio"] >= CONFIG["volume_spike_min_ratio"]]
    spikes = sorted(spikes, key=lambda c: c["vol_ratio"], reverse=True)[:n]

    inflow = sorted([c for c in chains if c["delta_24h"] > 0], key=lambda c: c["delta_24h"], reverse=True)[:7]
    outflow = sorted([c for c in chains if c["delta_24h"] < 0], key=lambda c: c["delta_24h"])[:5]
    inflow_7d = sorted([c for c in chains if c["prev_week"] > 0 and c["delta_7d"] > 0], key=lambda c: c["delta_7d"], reverse=True)[:5]

    # Tín hiệu hội tụ: ecosystem tăng MC + chain cùng tên có stablecoin chảy vào
    chain_map = {c["chain"].lower(): c for c in chains}
    confluence = []
    for e in ecos:
        key = e["name"].lower().replace(" ecosystem", "").strip()
        ch = chain_map.get(key)
        if ch and e["mcap_pct_24h"] > 0 and ch["delta_24h"] > 0:
            confluence.append((e, ch))
    confluence.sort(key=lambda t: t[0]["mcap_pct_24h"], reverse=True)

    tot_d = total["current"] - total["prev_day"]
    tot_w = total["current"] - total["prev_week"]

    s = []
    s.append(f"# 📊 Báo cáo dòng tiền theo sector — {now.strftime('%d/%m/%Y')}\n")
    s.append(f"_Cập nhật lúc {now.strftime('%H:%M')} (GMT+7) · Nguồn: CoinGecko, DefiLlama_\n")

    s.append("## ⚡ Tóm tắt nhanh\n")
    bullets = []
    if top_sectors:
        t = top_sectors[0]
        bullets.append(f"Sector tăng mạnh nhất: **{t['name']}** ({pct(t['mcap_pct_24h'])}, {usd(t['mcap_delta_usd'], True)})")
    if top_abs:
        t = top_abs[0]
        bullets.append(f"Hút nhiều vốn hóa nhất (tính theo $): **{t['name']}** ({usd(t['mcap_delta_usd'], True)})")
    if spikes:
        t = spikes[0]
        bullets.append(f"Volume đột biến nhất: **{t['name']}** ({t['vol_ratio']:.1f}x trung bình 7 ngày)")
    if inflow:
        t = inflow[0]
        bullets.append(f"Stablecoin chảy vào nhiều nhất: **{t['chain']}** ({usd(t['delta_24h'], True)} / 24h)")
    bullets.append(f"Tổng cung stablecoin USD: {usd(total['current'])} ({usd(tot_d, True)} 24h · {usd(tot_w, True)} 7 ngày)")
    s.append("\n".join(f"- {b}" for b in bullets) + "\n")

    s.append("## 🚀 Sector tăng vốn hóa mạnh nhất (24h)\n")
    s.append(md_table(cat_headers, [cat_row(i + 1, c) for i, c in enumerate(top_sectors)]))

    s.append("## 💰 Top sector hút vốn hóa nhiều nhất (tính theo $)\n")
    s.append(md_table(cat_headers, [cat_row(i + 1, c) for i, c in enumerate(top_abs)]))

    s.append("## 🔥 Đột biến volume (so với trung bình 7 ngày)\n")
    if history_days < CONFIG["volume_spike_min_days"]:
        s.append(f"_Mới có {history_days} ngày lịch sử — cần ít nhất {CONFIG['volume_spike_min_days']} ngày để tính. "
                 "Mục này sẽ tự xuất hiện sau vài lần chạy._\n")
    else:
        s.append(md_table(
            ["#", "Sector", "Volume 24h", "TB 7 ngày", "Gấp", "MC 24h"],
            [[str(i + 1), c["name"], usd(c["volume_24h"]), usd(c["vol_avg_7d"]),
              f"{c['vol_ratio']:.2f}x", pct(c["mcap_pct_24h"])] for i, c in enumerate(spikes)]))

    s.append("## 🌐 Hệ sinh thái (ecosystem) tăng mạnh nhất\n")
    s.append(md_table(cat_headers, [cat_row(i + 1, c) for i, c in enumerate(top_ecos)]))

    s.append("## 💵 Dòng stablecoin theo chain\n")
    s.append("**Chảy vào nhiều nhất 24h**\n")
    s.append(md_table(["#", "Chain", "Δ 24h", "% 24h", "Δ 7 ngày", "Tổng stablecoin"],
                      [[str(i + 1), c["chain"], usd(c["delta_24h"], True), pct(c["pct_24h"]),
                        (usd(c["delta_7d"], True) if c["prev_week"] else "—"), usd(c["current"])] for i, c in enumerate(inflow)]))
    s.append("**Rút ra nhiều nhất 24h**\n")
    s.append(md_table(["#", "Chain", "Δ 24h", "% 24h", "Δ 7 ngày", "Tổng stablecoin"],
                      [[str(i + 1), c["chain"], usd(c["delta_24h"], True), pct(c["pct_24h"]),
                        (usd(c["delta_7d"], True) if c["prev_week"] else "—"), usd(c["current"])] for i, c in enumerate(outflow)]))
    s.append("**Xu hướng 7 ngày — hút stablecoin mạnh nhất**\n")
    s.append(md_table(["#", "Chain", "Δ 7 ngày", "% 7 ngày", "Tổng stablecoin"],
                      [[str(i + 1), c["chain"], usd(c["delta_7d"], True), pct(c["pct_7d"]), usd(c["current"])]
                       for i, c in enumerate(inflow_7d)]))

    if confluence:
        s.append("\n## 🎯 Tín hiệu hội tụ\n")
        s.append("_Hệ sinh thái vừa tăng vốn hóa, vừa có stablecoin chảy vào chain tương ứng:_\n")
        s.append("\n".join(f"- **{e['name']}**: MC {pct(e['mcap_pct_24h'])} · stablecoin {usd(ch['delta_24h'], True)}"
                           for e, ch in confluence[:5]) + "\n")

    s.append("\n## 📉 Sector giảm mạnh nhất (24h)\n")
    s.append(md_table(cat_headers, [cat_row(i + 1, c) for i, c in enumerate(worst_sectors)]))

    s.append("\n---\n_Lưu ý: thay đổi market cap phản ánh cả biến động giá, không hoàn toàn là dòng tiền mới. "
             "Các category của CoinGecko chồng lấn nhau (1 coin có thể thuộc nhiều sector). "
             "Chỉ mang tính tham khảo, không phải lời khuyên đầu tư._\n")
    return "\n".join(s)


# ───────────────────────────── Snapshot ─────────────────────────────
def save_snapshot(today: str, cats: list[dict], chains: list[dict]) -> None:
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    snap = {
        "date": today,
        "categories": {c["id"]: {"name": c["name"], "mcap": c["mcap"], "volume_24h": c["volume_24h"],
                                 "mcap_pct_24h": c["mcap_pct_24h"]} for c in cats},
        "stablecoin_chains": {c["chain"]: c["current"] for c in chains},
    }
    (HISTORY_DIR / f"{today}.json").write_text(json.dumps(snap, ensure_ascii=False, indent=1))

    cutoff = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=CONFIG["history_keep_days"])).strftime("%Y-%m-%d")
    for f in HISTORY_DIR.glob("*.json"):
        if f.stem < cutoff:
            f.unlink()


# ───────────────────────────── Main ─────────────────────────────
def run(categories_raw=None, stable_raw=None, now: datetime | None = None) -> str:
    now = now or datetime.now(VN_TZ)
    today = now.strftime("%Y-%m-%d")

    categories_raw = categories_raw if categories_raw is not None else fetch_categories()
    stable_raw = stable_raw if stable_raw is not None else fetch_stablecoins()

    cats = clean_categories(categories_raw)
    history = load_history(today)
    add_volume_spikes(cats, history)
    chains, total = stablecoin_flows(stable_raw)

    report = build_report(cats, chains, total, now, history_days=len(history))

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    (REPORT_DIR / f"{today}.md").write_text(report)
    (REPORT_DIR / "latest.md").write_text(report)
    save_snapshot(today, cats, chains)
    return report


if __name__ == "__main__":
    try:
        print(run())
    except Exception as e:  # để GitHub Actions báo đỏ rõ ràng
        print(f"❌ Lỗi: {e}", file=sys.stderr)
        sys.exit(1)
