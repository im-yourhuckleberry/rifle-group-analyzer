"""Cartridge name to bullet diameter (inches) table used to pre-fill new rifles."""

CALIBERS: dict[str, float] = {
    ".223 Rem": 0.224,
    "5.56": 0.224,
    "22 Creedmoor": 0.224,
    "22-250": 0.224,
    "6mm ARC": 0.243,
    "6mm Creedmoor": 0.243,
    ".243 Win": 0.243,
    "6UM": 0.243,
    "6.5 Creedmoor": 0.264,
    "6.5 PRC": 0.264,
    ".270 Win": 0.277,
    "7mm PRC": 0.284,
    "7mm Rem Mag": 0.284,
    ".308 Win": 0.308,
    ".300 Win Mag": 0.308,
    ".300 PRC": 0.308,
    ".338": 0.338,
}

DIAMETER_CHOICES: tuple[float, ...] = (0.224, 0.243, 0.257, 0.264, 0.277, 0.284, 0.308, 0.338)
"""The bullet diameters (inches) the rifle forms offer (B-82); the API accepts the wider range."""

_BY_NORMALIZED_NAME = {name.casefold(): d for name, d in CALIBERS.items()}


def diameter_for(cartridge: str) -> float | None:
    """Bullet diameter in inches for `cartridge` (case/outer-whitespace insensitive), or None if unknown."""
    return _BY_NORMALIZED_NAME.get(cartridge.strip().casefold())
