#!/usr/bin/env python3
"""Cost compare for workload B: :8500 TCO vs SiliconFlow bill vs Bailian-CN list.

Uses the same TCO math as workload_b_compare.py. Neolink has no published
unit price; Bailian CN qwen3.6-flash (≤256K) is a reference only.
Does not send traffic. Does not read API keys.
"""
from __future__ import annotations

CAPEX_LOW = 360000.0
CAPEX_HIGH = 440000.0
YUAN_KWH = 1.0
SCENARIOS = [
    ("mid", CAPEX_LOW, 3, 0.60, 2.0),
    ("dear", CAPEX_HIGH, 2, 0.30, 2.4),
    ("cheap", CAPEX_LOW, 3, 0.90, 1.6),
]
SF_IN, SF_OUT = 1.80, 10.80
# 百炼华北2 中国内地 qwen3.6-flash，0<token≤256K
# https://help.aliyun.com/zh/model-studio/qwen3-6-flash
BL_IN, BL_OUT = 1.20, 7.20

LOCAL = [
    # conc, qps, done, wall, prompt, completion, power_w
    (1, 0.3140605899845162, 189, 601.795, 32912.57142857143, 184.0, 647.1467520661157),
    (8, 0.8397491565540838, 507, 603.752, 32776.686390532544, 183.9723865877712, 806.352611570248),
    (32, 1.2602345107616226, 761, 603.856, 32916.76478318003, 183.55059132720106, 942.1367272727273),
    (80, 1.6188069273934622, 978, 604.149, 32825.41104294478, 183.21574642126788, 1106.2021157024794),
]
SF = [
    (1, 0.09745862064390032, 59, 605.385, 32632.237288135595, 184.0, 3.582781),
    (8, 0.08087790540697891, 50, 618.216, 32508.8, 184.0, 3.025146),
    (32, 0.1340273707308691, 87, 649.121, 32591.103448275862, 184.0, 5.276645),
    (80, 0.2371775495516132, 179, 754.709, 32658.893854748603, 184.0, 10.878388),
]
NL = [
    (1, 0.38142175463024147, 229, 600.385, 32835.91266375546, 183.4890829694323),
    (8, 2.9736170584910093, 1790, 601.96, 32826.08268156424, 183.5122905027933),
    (32, 4.035286073479413, 3314, 821.255, 33080.800241400124, 183.7386843693422),
    (80, 4.132744027883052, 3353, 811.325, 33154.073963614675, 183.71637339695795),
]


def hours_year(years: float, util: float) -> float:
    return years * 365.25 * 24.0 * util


def tco_hourly(capex: float, years: float, util: float, kw: float) -> float:
    return capex / hours_year(years, util) + kw * YUAN_KWH


def tco_req(hourly: float, qps: float) -> float:
    return hourly / (qps * 3600.0)


def token_bill(prompt: float, completion: float, pin: float, pout: float) -> float:
    return prompt / 1e6 * pin + completion / 1e6 * pout


def main() -> None:
    hourly = {name: tco_hourly(c, y, u, kw) for name, c, y, u, kw in SCENARIOS}
    amort_mid = CAPEX_LOW / hours_year(3, 0.60)
    print("## hourly TCO (busy hour)")
    for name, h in hourly.items():
        print(f"  {name:6} ¥{h:.3f}/h")
    print(f"  mid_amort_only ¥{amort_mid:.3f}/h")

    print("\n## ¥/successful request at measured QPS")
    print("conc  local_qps  mid  dear  cheap  mid+measW  sf_list  bailian  sf_paid")
    sf_paid = {c: bill / n for c, _, n, _, _, _, bill in SF}
    for conc, qps, n, wall, pt, ct, pw in LOCAL:
        mid, dear, cheap = (tco_req(hourly[s], qps) for s in ("mid", "dear", "cheap"))
        mid_m = tco_req(amort_mid + pw / 1000.0 * YUAN_KWH, qps)
        sf_u = token_bill(pt, ct, SF_IN, SF_OUT)
        bl_u = token_bill(pt, ct, BL_IN, BL_OUT)
        print(
            f"{conc:4d}  {qps:8.3f}  {mid:.4f}  {dear:.4f}  {cheap:.4f}  "
            f"{mid_m:.4f}  {sf_u:.4f}  {bl_u:.4f}  {sf_paid.get(conc, float('nan')):.4f}"
        )

    print("\n## cloud own operating point (token ¥/req, not 236 TCO)")
    print("target conc  qps  sf_or_paid  bailian_ref")
    for conc, qps, n, wall, pt, ct, bill in SF:
        print(f"sf     {conc:3d}  {qps:.3f}  paid={bill/n:.4f}  list={token_bill(pt,ct,SF_IN,SF_OUT):.4f}")
    for conc, qps, n, wall, pt, ct in NL:
        print(f"nl     {conc:3d}  {qps:.3f}  bailian={token_bill(pt,ct,BL_IN,BL_OUT):.4f}  (not Neolink invoice)")

    print("\n## break-even QPS (236 cheaper below this? no: cheaper ABOVE this QPS)")
    sf_unit = 0.060693  # 22.763/375
    # representative B token
    bl_unit = token_bill(32850, 184, BL_IN, BL_OUT)
    print(f"sf_paid_unit={sf_unit:.4f}  bailian_unit={bl_unit:.4f}")
    for name, h in hourly.items():
        print(f"  {name:6} vs SF {h/(sf_unit*3600):.3f} QPS   vs bailian {h/(bl_unit*3600):.3f} QPS")

    print("\n## 1 hour at local c80 QPS 1.619")
    qps80 = LOCAL[3][1]
    n_h = qps80 * 3600
    print(f"  requests={n_h:.0f}")
    print(f"  236 mid     ¥{hourly['mid']:.2f}  ({tco_req(hourly['mid'], qps80):.4f}/req)")
    print(f"  236 dear    ¥{hourly['dear']:.2f}  ({tco_req(hourly['dear'], qps80):.4f}/req)")
    print(f"  236 cheap   ¥{hourly['cheap']:.2f}  ({tco_req(hourly['cheap'], qps80):.4f}/req)")
    print(f"  SF if could ¥{n_h * sf_unit:.2f}  (this key max QPS 0.24, cannot)")
    print(f"  bailian ref ¥{n_h * bl_unit:.2f}  (Neolink measured 4.13 QPS, can)")

    print("\n## 1000 successful B")
    print(f"  236 c80 mid ¥{1000 * tco_req(hourly['mid'], qps80):.2f}")
    print(f"  SF paid     ¥{1000 * sf_unit:.2f}")
    print(f"  bailian ref ¥{1000 * bl_unit:.2f}")

    print("\n## experiment window cost (4x ~10min occupied)")
    win_h = sum(r[3] for r in LOCAL) / 3600.0
    print(f"  wall_h={win_h:.3f}  236 mid ¥{hourly['mid'] * win_h:.2f}")
    print(f"  SF actual 4 levels ¥{sum(r[6] for r in SF):.2f} for {sum(r[2] for r in SF)} ok")
    nl_tok = sum(r[2] * token_bill(r[4], r[5], BL_IN, BL_OUT) for r in NL)
    print(f"  NL bailian-ref 4 levels ¥{nl_tok:.2f} for {sum(r[2] for r in NL)} ok")


if __name__ == "__main__":
    main()
