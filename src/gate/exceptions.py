"""
Budget guard exceptions for gate.
"""

__all__ = [
    "CostBudgetExceeded",
    "LoopBudgetExceeded",
]
class CostBudgetExceeded(Exception):
    """Raised when cost budget is exceeded."""
    pass


class LoopBudgetExceeded(Exception):
    """Raised when iteration/loop budget is exceeded."""
    pass
