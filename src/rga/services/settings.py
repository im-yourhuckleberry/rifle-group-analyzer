"""Stored settings: the share-card defaults (the first stored setting).

`share_defaults` holds six booleans, one per share-card layer. With no stored row the built-in
defaults apply: photo, spread and numbers on, the rest off. Reading never fails on an odd row: a
non-object document, or a missing or non-boolean entry, falls back to the built-in value. Writing
validates the full set.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping

from rga.db import repo_settings
from rga.services.groups import GroupError, _write_transaction

__all__ = [
    "BUILT_IN_SHARE_DEFAULTS",
    "SHARE_DEFAULTS_KEY",
    "SHARE_LAYERS",
    "SettingsError",
    "get_share_defaults",
    "set_share_defaults",
]

SHARE_DEFAULTS_KEY = "share_defaults"
SHARE_LAYERS = ("photo", "spread", "thing_hitter", "poi", "correction", "numbers")
BUILT_IN_SHARE_DEFAULTS: dict[str, bool] = {
    "photo": True,
    "spread": True,
    "thing_hitter": False,
    "poi": False,
    "correction": False,
    "numbers": True,
}


class SettingsError(GroupError):
    """A setting could not be saved; `code` is a stable snake_case key (`invalid_setting`)."""


def get_share_defaults(conn: sqlite3.Connection) -> dict[str, bool]:
    """The six share-card defaults: the stored ones where valid, the built-in ones otherwise."""
    row = repo_settings.get_setting(conn, SHARE_DEFAULTS_KEY)
    stored = None if row is None else json.loads(row.value)
    if not isinstance(stored, dict):
        return dict(BUILT_IN_SHARE_DEFAULTS)
    return {
        layer: stored[layer] if isinstance(stored.get(layer), bool) else built_in
        for layer, built_in in BUILT_IN_SHARE_DEFAULTS.items()
    }


def set_share_defaults(conn: sqlite3.Connection, values: Mapping[str, object]) -> dict[str, bool]:
    """Store `values` (exactly the six layers, each a bool) and return what is now stored.

    Raises `SettingsError` `invalid_setting` for an unknown or missing layer or a non-bool value;
    nothing is written then.
    """
    unknown = sorted(set(values) - set(SHARE_LAYERS))
    missing = [layer for layer in SHARE_LAYERS if layer not in values]
    bad = [layer for layer in SHARE_LAYERS if layer in values and type(values[layer]) is not bool]
    if unknown or missing or bad:
        raise SettingsError(
            "invalid_setting",
            "share defaults take exactly "
            + ", ".join(SHARE_LAYERS)
            + ", each true or false"
            + (f" (unknown: {', '.join(unknown)})" if unknown else ""),
        )
    clean = {layer: bool(values[layer]) for layer in SHARE_LAYERS}
    with _write_transaction(conn):
        repo_settings.put_setting(conn, SHARE_DEFAULTS_KEY, json.dumps(clean))
    return clean
