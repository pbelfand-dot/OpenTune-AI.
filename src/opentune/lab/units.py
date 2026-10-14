"""Unit conversions used throughout the lab.

Internal calculations are SI (Pa, K, kg, s, m, W, N·m, rad/s). Engine speed is
handled as revolutions per second (``n``, 1/s) inside formulas and as rpm at the
user interface. Power conversions use the exact definitions:

* 1 mechanical horsepower (hp) = 745.69987158227022 W (550 ft·lbf/s)
* 1 metric horsepower (PS)     = 735.49875 W (75 kgf·m/s)
* hp = lb·ft × rpm / 5252.113…  (33000 / 2π)
"""

from __future__ import annotations

import math

W_PER_HP = 745.69987158227022
W_PER_PS = 735.49875
NM_PER_LBFT = 1.3558179483314004
PA_PER_PSI = 6894.757293168361
PA_PER_BAR = 1.0e5
HP_RPM_CONSTANT = 33000.0 / (2.0 * math.pi)  # 5252.113...
KELVIN_OFFSET = 273.15


def rpm_to_rps(rpm: float) -> float:
    return rpm / 60.0


def rps_to_rpm(rps: float) -> float:
    return rps * 60.0


def rpm_to_rad_s(rpm: float) -> float:
    return rpm * 2.0 * math.pi / 60.0


def rad_s_to_rpm(omega: float) -> float:
    return omega * 60.0 / (2.0 * math.pi)


def c_to_k(t_c: float) -> float:
    return t_c + KELVIN_OFFSET


def k_to_c(t_k: float) -> float:
    return t_k - KELVIN_OFFSET


def kw_to_hp(kw: float) -> float:
    return kw * 1000.0 / W_PER_HP


def kw_to_ps(kw: float) -> float:
    return kw * 1000.0 / W_PER_PS


def nm_to_lbft(nm: float) -> float:
    return nm / NM_PER_LBFT


def pa_to_psi(pa: float) -> float:
    return pa / PA_PER_PSI


def power_w(torque_nm: float, rpm: float) -> float:
    """Shaft power P = T·ω."""
    return torque_nm * rpm_to_rad_s(rpm)


def hp_from_lbft_rpm(torque_lbft: float, rpm: float) -> float:
    return torque_lbft * rpm / HP_RPM_CONSTANT
