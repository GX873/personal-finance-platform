"""Portfolio statistics exposed behind a small xalpha-compatible boundary."""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal


class XalphaAdapter:
    def __init__(self, values: Sequence[Decimal | int | float]):
        self.values = tuple(Decimal(str(value)) for value in values)
        if not self.values or any(value <= 0 for value in self.values):
            raise ValueError("NAV history must contain positive values")

    def total_return(self) -> Decimal:
        if len(self.values) < 2:
            return Decimal(0)
        return self.values[-1] / self.values[0] - Decimal(1)

    def max_drawdown(self) -> Decimal:
        peak = self.values[0]
        worst = Decimal(0)
        for value in self.values:
            peak = max(peak, value)
            worst = min(worst, value / peak - Decimal(1))
        return worst

    def volatility(self) -> Decimal:
        if len(self.values) < 3:
            return Decimal(0)
        returns = [
            self.values[index] / self.values[index - 1] - 1
            for index in range(1, len(self.values))
        ]
        mean = sum(returns, Decimal(0)) / len(returns)
        variance = sum((value - mean) ** 2 for value in returns) / (
            len(returns) - 1
        )
        return Decimal(variance).sqrt()
