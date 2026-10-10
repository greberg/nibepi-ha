"""Tiny dense linear algebra for the model fits (no numpy).

The fits have at most a handful of parameters, so plain lists and
Gaussian elimination with partial pivoting are fast enough and exact
enough. Everything is deterministic.
"""

from __future__ import annotations

from typing import List, Optional, Sequence

Matrix = List[List[float]]
Vector = List[float]


def zeros(rows: int, cols: int) -> Matrix:
    return [[0.0] * cols for _ in range(rows)]


def solve(a: Sequence[Sequence[float]], b: Sequence[float]) -> Optional[Vector]:
    """Solve ``a x = b``; None when ``a`` is (numerically) singular."""
    n = len(a)
    m = [list(map(float, row)) + [float(b[i])] for i, row in enumerate(a)]
    scale = max((abs(v) for row in a for v in row), default=0.0) or 1.0
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(m[r][col]))
        if abs(m[pivot][col]) <= 1e-12 * scale:
            return None
        if pivot != col:
            m[col], m[pivot] = m[pivot], m[col]
        p = m[col][col]
        for r in range(col + 1, n):
            f = m[r][col] / p
            if f:
                row_r, row_c = m[r], m[col]
                for k in range(col, n + 1):
                    row_r[k] -= f * row_c[k]
    x = [0.0] * n
    for r in range(n - 1, -1, -1):
        s = m[r][n] - sum(m[r][k] * x[k] for k in range(r + 1, n))
        x[r] = s / m[r][r]
    return x


def inverse(a: Sequence[Sequence[float]]) -> Optional[Matrix]:
    n = len(a)
    columns = []
    for i in range(n):
        e = [0.0] * n
        e[i] = 1.0
        col = solve(a, e)
        if col is None:
            return None
        columns.append(col)
    return [[columns[j][i] for j in range(n)] for i in range(n)]


class NormalEquations:
    """Running ``XᵀX`` / ``Xᵀy`` sums with optional exponential forgetting.

    Adding rows one by one with ``forget`` < 1 is recursive least squares
    with a forgetting factor; with ``forget`` = 1 it is ordinary windowed
    least squares over everything added. ``solve`` adds a ridge pull
    towards ``prior`` (weights ``ridge``), which keeps the fit sane when the
    data does not excite every parameter.
    """

    def __init__(self, size: int, forget: float = 1.0) -> None:
        self.size = size
        self.forget = forget
        self.xtx = zeros(size, size)
        self.xty = [0.0] * size
        self.yty = 0.0
        self.count = 0.0  # effective number of rows
        self.rows = 0

    def add(self, x: Sequence[float], y: float, weight: float = 1.0) -> None:
        f = self.forget
        if f != 1.0:
            for i in range(self.size):
                row = self.xtx[i]
                for j in range(self.size):
                    row[j] *= f
                self.xty[i] *= f
            self.yty *= f
            self.count *= f
        for i in range(self.size):
            xi = x[i] * weight
            if xi:
                row = self.xtx[i]
                for j in range(self.size):
                    row[j] += xi * x[j]
            self.xty[i] += xi * y
        self.yty += weight * y * y
        self.count += weight
        self.rows += 1

    def solve(
        self, ridge: Optional[Sequence[float]] = None, prior: Optional[Sequence[float]] = None
    ) -> Optional[Vector]:
        a = [row[:] for row in self.xtx]
        b = self.xty[:]
        if ridge is not None:
            for i, lam in enumerate(ridge):
                a[i][i] += lam
                if prior is not None:
                    b[i] += lam * prior[i]
        return solve(a, b)

    def residual_sum(self, theta: Sequence[float]) -> float:
        """Σ(y - xθ)² from the sums (θᵀXᵀXθ - 2θᵀXᵀy + yᵀy)."""
        n = self.size
        quad = sum(theta[i] * self.xtx[i][j] * theta[j] for i in range(n) for j in range(n))
        lin = sum(theta[i] * self.xty[i] for i in range(n))
        return max(0.0, quad - 2.0 * lin + self.yty)
