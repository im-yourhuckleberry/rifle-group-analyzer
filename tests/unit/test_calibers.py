import pytest

from rga.domain.calibers import CALIBERS, diameter_for

REQUIRED = [
    ".223 Rem", "5.56", "22 Creedmoor", "22-250", "6mm ARC", "6mm Creedmoor", ".243 Win",
    "6UM", "6.5 Creedmoor", "6.5 PRC", ".270 Win", "7mm PRC", "7mm Rem Mag", ".308 Win",
    ".300 Win Mag", ".300 PRC", ".338",
]  # fmt: skip


def test_table_covers_design_list():
    assert set(REQUIRED) <= set(CALIBERS)


@pytest.mark.parametrize(
    ("cartridge", "diameter"),
    [
        (".223 Rem", 0.224),
        ("6.5 Creedmoor", 0.264),
        ("6mm ARC", 0.243),
        (".308 Win", 0.308),
        (".300 PRC", 0.308),
        (".270 Win", 0.277),
        ("7mm PRC", 0.284),
        (".338", 0.338),
    ],
)
def test_known_diameters(cartridge, diameter):
    assert diameter_for(cartridge) == diameter


def test_all_diameters_are_plausible_bullet_sizes():
    assert all(0.2 < d < 0.4 for d in CALIBERS.values())


def test_lookup_ignores_case_and_outer_whitespace():
    assert diameter_for("  6.5 creedmoor ") == 0.264


@pytest.mark.parametrize("name", ["", "   ", "9mm", "not a caliber"])
def test_unknown_cartridge_is_none(name):
    assert diameter_for(name) is None
