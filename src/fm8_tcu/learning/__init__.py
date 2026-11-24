# -*- coding: utf-8 -*-
"""车辆学习：齿比、扭矩曲线、断油转速。"""

from .gear_ratio import GearRatioTable
from .power_curve import TorqueCurve
from .rev_limiter import RevLimiter
from .vehicle import VehicleModel

__all__ = ["GearRatioTable", "TorqueCurve", "RevLimiter", "VehicleModel"]
