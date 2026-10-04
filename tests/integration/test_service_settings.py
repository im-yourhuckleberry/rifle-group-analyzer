"""T1/T2: the settings repository and the share-defaults service against a real migrated database."""

from __future__ import annotations

import json
import sqlite3

import pytest

from rga.db import repo_settings
from rga.services import settings as service
from tests.support.db import migrated_conn  # noqa: F401

BUILT_IN = {
    "photo": True,
    "spread": True,
    "thing_hitter": False,
    "poi": False,
    "correction": False,
    "numbers": True,
}


def rows(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    return [tuple(r) for r in conn.execute("SELECT key, value FROM settings ORDER BY key")]


class TestRepository:
    def test_a_missing_key_is_none(self, migrated_conn) -> None:  # noqa: F811
        assert repo_settings.get_setting(migrated_conn, "nope") is None

    def test_put_inserts_then_replaces_one_row_and_stamps_the_time(self, migrated_conn) -> None:  # noqa: F811
        first = repo_settings.put_setting(
            migrated_conn, "k", '{"a": 1}', now="2026-10-02T10:00:00Z"
        )
        second = repo_settings.put_setting(
            migrated_conn, "k", '{"a": 2}', now="2026-10-02T11:00:00Z"
        )

        assert (first.value, second.value) == ('{"a": 1}', '{"a": 2}')
        assert repo_settings.get_setting(migrated_conn, "k") == second
        assert second.updated_at == "2026-10-02T11:00:00Z"
        assert migrated_conn.execute("SELECT COUNT(*) FROM settings").fetchone()[0] == 1

    def test_put_uses_the_current_utc_time_by_default(self, migrated_conn) -> None:  # noqa: F811
        row = repo_settings.put_setting(migrated_conn, "k", "{}")

        assert row.updated_at.endswith("Z") and row.updated_at.startswith("20")

    def test_put_does_not_commit_and_rejects_invalid_json(self, migrated_conn) -> None:  # noqa: F811
        repo_settings.put_setting(migrated_conn, "k", "{}")
        assert migrated_conn.in_transaction
        migrated_conn.rollback()
        assert rows(migrated_conn) == []
        with pytest.raises(sqlite3.IntegrityError):
            repo_settings.put_setting(migrated_conn, "k", "not json")

    def test_keys_are_independent(self, migrated_conn) -> None:  # noqa: F811
        repo_settings.put_setting(migrated_conn, "a", '{"x": 1}')
        repo_settings.put_setting(migrated_conn, "b", '{"x": 2}')

        assert repo_settings.get_setting(migrated_conn, "a").value == '{"x": 1}'
        assert repo_settings.get_setting(migrated_conn, "b").value == '{"x": 2}'


class TestShareDefaults:
    def test_with_no_row_the_built_in_defaults_apply(self, migrated_conn) -> None:  # noqa: F811
        assert service.get_share_defaults(migrated_conn) == BUILT_IN
        assert service.BUILT_IN_SHARE_DEFAULTS == BUILT_IN
        assert list(service.get_share_defaults(migrated_conn)) == list(service.SHARE_LAYERS)

    def test_set_stores_exactly_the_six_booleans_and_commits(self, migrated_conn) -> None:  # noqa: F811
        wanted = {**BUILT_IN, "poi": True, "numbers": False}

        returned = service.set_share_defaults(migrated_conn, wanted)

        assert returned == wanted and not migrated_conn.in_transaction
        assert [(k, json.loads(v)) for k, v in rows(migrated_conn)] == [("share_defaults", wanted)]
        assert service.get_share_defaults(migrated_conn) == wanted

    def test_set_replaces_the_whole_document(self, migrated_conn) -> None:  # noqa: F811
        service.set_share_defaults(migrated_conn, {**BUILT_IN, "poi": True})
        service.set_share_defaults(migrated_conn, BUILT_IN)

        assert service.get_share_defaults(migrated_conn) == BUILT_IN
        assert len(rows(migrated_conn)) == 1

    @pytest.mark.parametrize(
        "bad",
        [
            {**BUILT_IN, "photo": 1},
            {**BUILT_IN, "photo": 0},
            {**BUILT_IN, "photo": "true"},
            {**BUILT_IN, "photo": None},
            {**BUILT_IN, "extra": True},
            {k: v for k, v in BUILT_IN.items() if k != "numbers"},
            {},
        ],
    )
    def test_set_refuses_unknown_missing_and_non_bool_values_and_writes_nothing(
        self,
        migrated_conn,  # noqa: F811
        bad,
    ) -> None:
        with pytest.raises(service.SettingsError) as caught:
            service.set_share_defaults(migrated_conn, bad)

        assert caught.value.code == "invalid_setting"
        assert rows(migrated_conn) == []

    def test_a_failed_set_leaves_the_previous_value(self, migrated_conn) -> None:  # noqa: F811
        saved = {**BUILT_IN, "poi": True}
        service.set_share_defaults(migrated_conn, saved)

        with pytest.raises(service.SettingsError):
            service.set_share_defaults(migrated_conn, {**saved, "poi": "no"})

        assert service.get_share_defaults(migrated_conn) == saved

    @pytest.mark.parametrize(
        ("stored", "expected"),
        [
            ('["a"]', BUILT_IN),
            ("5", BUILT_IN),
            ("null", BUILT_IN),
            ('"photo"', BUILT_IN),
            ("{}", BUILT_IN),
            ('{"photo": false}', {**BUILT_IN, "photo": False}),
            ('{"poi": true, "extra": true}', {**BUILT_IN, "poi": True}),
            ('{"photo": 0, "poi": "yes", "numbers": null}', BUILT_IN),
        ],
    )
    def test_reading_an_odd_stored_document_falls_back_per_layer(
        self,
        migrated_conn,  # noqa: F811
        stored,
        expected,
    ) -> None:
        repo_settings.put_setting(migrated_conn, service.SHARE_DEFAULTS_KEY, stored)

        assert service.get_share_defaults(migrated_conn) == expected
