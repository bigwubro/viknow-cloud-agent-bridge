#!/usr/bin/env python3
"""Workload B cost compare: 236 card amort only vs cloud token + limiter.

Electricity is excluded. Neolink has no list price; Bailian CN qwen3.6-flash
(≤256K) is reference only. No traffic, no API keys.
"""
from __future__ import annotations

CAPEX_LOW = 360000.0
CAPEX_HIGH = 440000.0
# card only: years, util. kW is ignored.
SCENARIOS = [
    ("mid", CAPEX_LOW, 3, 0.60),
    ("dear", CAPEX_HIGH, 2, 0.30),
    ("cheap", CAPEX_LOW, 3, 0.90),
]
SF_IN, SF_OUT = 1.80, 10.80
BL_IN, BL_OUT = 1.20, 7.20

LOCAL = [
    # conc, qps, done, wall, prompt, completion
    (1, 0.3140605899845162, 189, 601.795, 32912.57142857143, 184.0),
    (8, 0.8397491565540838, 507, 603.752, 32776.686390532544, 183.9723865877712),
    (32, 1.2602345107616226, 761, 603.856, 32916.76478318003, 183.55059132720106),
    (80, 1.6188069273934622, 978, 604.149, 32825.41104294478, 183.21574642126788),
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


def card_hourly(capex: float, years: float, util: float) -> float:
    return capex / hours_year(years, util)


def tco_req(hourly: float, qps: float) -> float:
    return hourly / (qps * 3600.0)


def token_bill(prompt: float, completion: float, pin: float, pout: float) -> float:
    return prompt / 1e6 * pin + completion / 1e6 * pout


def tpm(qps: float, prompt: float, completion: float) -> float:
    return qps * (prompt + completion) * 60.0


def main() -> None:
    hourly = {name: card_hourly(c, y, u) for name, c, y, u in SCENARIOS}
    sf_unit = 22.76296 / 375.0
    bl_unit = token_bill(32850, 184, BL_IN, BL_OUT)
    print("## 1 card-only busy-hour ¥ (no power)")
    for name, h in hourly.items():
        print(f"  {name:6} ¥{h:.3f}/h")

    print("\n## 2 sustainable success QPS / equiv TPM (this B)")
    print("target  conc   qps   tok/req   TPM")
    for conc, qps, n, wall, pt, ct in LOCAL:
        print(f"8500    {conc:3d}  {qps:6.3f}  {pt+ct:7.0f}  {tpm(qps,pt,ct)/1e6:5.2f}M")
    for conc, qps, n, wall, pt, ct, bill in SF:
        print(f"sf      {conc:3d}  {qps:6.3f}  {pt+ct:7.0f}  {tpm(qps,pt,ct)/1e6:5.2f}M")
    for conc, qps, n, wall, pt, ct in NL:
        print(f"nl      {conc:3d}  {qps:6.3f}  {pt+ct:7.0f}  {tpm(qps,pt,ct)/1e6:5.2f}M")

    print("\n## 3 ¥/success at own operating point (236=card only)")
    print("conc  qps8500  mid  dear  cheap  sf_paid  bailian")
    sf_paid = {c: bill / n for c, _, n, _, _, _, bill in SF}
    for conc, qps, n, wall, pt, ct in LOCAL:
        print(
            f"{conc:4d}  {qps:7.3f}  {tco_req(hourly['mid'],qps):.4f}  "
            f"{tco_req(hourly['dear'],qps):.4f}  {tco_req(hourly['cheap'],qps):.4f}  "
            f"{sf_paid[conc]:.4f}  {token_bill(pt,ct,BL_IN,BL_OUT):.4f}"
        )

    print("\n## 4 break-even QPS (236 cheaper ABOVE this)")
    print(f"sf_paid={sf_unit:.4f}  bailian={bl_unit:.4f}")
    for name, h in hourly.items():
        print(f"  {name:6} vs SF {h/(sf_unit*3600):.3f}   vs bailian {h/(bl_unit*3600):.3f}")

    q_sf = SF[3][1]
    q_nl = NL[3][1]
    q_lo = LOCAL[3][1]
    print("\n## 5 copies needed (linear, if limits add)")
    print(f"  match 8500 {q_lo:.3f} QPS:  SF keys {q_lo/q_sf:.1f}   NL keys {q_lo/q_nl:.2f}   8500 x1")
    print(f"  match NL   {q_nl:.3f} QPS:  SF keys {q_nl/q_sf:.1f}   NL keys 1      8500 x{q_nl/q_lo:.2f} (4gpu sets)")

    print("\n## 6 1h at 8500 c80 QPS (card only)")
    n_h = q_lo * 3600
    print(f"  reqs={n_h:.0f}  236 mid ¥{hourly['mid']:.2f}  SF-if-could ¥{n_h*sf_unit:.2f}  bailian ¥{n_h*bl_unit:.2f}")
    print(f"  1000 ok: 236 ¥{1000*tco_req(hourly['mid'],q_lo):.2f}  SF ¥{1000*sf_unit:.2f}  bailian ¥{1000*bl_unit:.2f}")


if __name__ == "__main__":
    main()
