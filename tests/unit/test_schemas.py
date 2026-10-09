"""T1 tests for the API schemas: valid and invalid payloads per model, domain-type conformance,
the route registry against design §4.7, and the error shape against static/js/api.js."""

import re
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, TypeAdapter, ValidationError

from rga.db import rows
from rga.domain import types as dt_types
from rga.web import schemas as s

ROOT = Path(__file__).resolve().parents[2]
DESIGN = ROOT / ".claude" / "specs" / "rifle-group-analyzer" / "design.md"
API_JS = ROOT / "src" / "rga" / "web" / "static" / "js" / "api.js"

SHOT = {"x_in": 1.0, "y_in": 2.0, "origin": "detected"}
REVIEW = {"aim": {"x_in": 0.0, "y_in": 0.0}, "shots": [SHOT], "rejections": [], "shots_expected": 1}
GROUP = {
    "load_id": 1,
    "lot_code": "LOT-7",
    "shots_expected": 10,
    "shot_date": "2026-09-30",
    "distance_yd": 100,
    "position": "bench",
    "suppressor": False,
}
CIRCLE_MANUAL = {
    "edge_points_px": [{"x_px": 1, "y_px": 2}, {"x_px": 3, "y_px": 4}, {"x_px": 5, "y_px": 6}]
}
GRID_MANUAL = {
    "grid": {
        "corners_px": [
            {"x_px": 0, "y_px": 0},
            {"x_px": 9, "y_px": 0},
            {"x_px": 9, "y_px": 9},
            {"x_px": 0, "y_px": 9},
        ],
        "squares_x": 8,
        "squares_y": 8,
    }
}


def ok(model: type[BaseModel], payload: dict[str, Any]) -> BaseModel:
    return model.model_validate(payload)


def bad(model: type[BaseModel], payload: dict[str, Any]) -> ValidationError:
    with pytest.raises(ValidationError) as info:
        model.model_validate(payload)
    return info.value


# ----------------------------------------------------------------------------------- registry


def _design_routes() -> set[tuple[str, str]]:
    section = DESIGN.read_text().split("### 4.7 HTTP API")[1].split("Pages:")[0]
    found: set[tuple[str, str]] = set()
    pattern = r"`((?:GET|POST|PUT|PATCH|DELETE)(?:,(?:GET|POST|PUT|PATCH|DELETE))*) (/[^`\s?]*)"
    for match in re.finditer(pattern, section):
        found.update((method, match.group(2)) for method in match.group(1).split(","))
    return found


def test_registry_covers_exactly_the_routes_in_design_4_7() -> None:
    design = _design_routes()
    assert len(design) == 49, "design §4.7 parse looks wrong"
    assert set(s.ROUTE_SCHEMAS) == design


def test_detach_route_is_registered_with_a_zero_response_and_no_body() -> None:
    route = s.ROUTE_SCHEMAS[("DELETE", "/api/zeros/{id}/groups/{gid}")]
    assert route.response is s.ZeroOut
    assert route.request is None and route.query is None


def test_reference_reset_route_has_no_body_and_returns_the_photo_and_a_count() -> None:
    route = s.ROUTE_SCHEMAS[("POST", "/api/photos/{id}/reference/reset")]
    assert route.request is None and route.query is None
    assert route.response is s.ReferenceResetOut
    assert set(s.ReferenceResetOut.model_fields) == {"photo", "cleared_groups"}
    with pytest.raises(ValidationError):
        s.ReferenceResetOut.model_validate({"photo": None, "cleared_groups": -1})


def test_group_out_carries_a_masked_flag_that_detail_inherits() -> None:
    assert s.GroupOut.model_fields["masked"].annotation is bool
    assert s.GroupOut.model_fields["masked"].default is False
    assert "masked" in s.GroupDetailOut.model_fields


def test_every_body_route_has_a_request_model_and_every_get_none() -> None:
    body_methods = {"POST", "PUT", "PATCH"}
    no_body_posts = {
        ("POST", "/api/photos"),  # multipart upload
        ("POST", "/api/groups/{id}/fill-shortfall"),
        ("POST", "/api/groups/{id}/finalize"),
        ("POST", "/api/photos/{id}/reference/reset"),
    }
    for (method, path), route in s.ROUTE_SCHEMAS.items():
        if method in body_methods and (method, path) not in no_body_posts:
            assert route.request is not None, (method, path)
        if method == "GET":
            assert route.request is None, (method, path)


# Routes with no JSON response body: the two binary downloads (the photo image file and the
# group share card, both `FileResponse`/`Response` with an image media type) and a 204 delete.
NO_RESPONSE_BODY = {
    ("GET", "/api/photos/{id}/image"),
    ("GET", "/api/groups/{id}/share.png"),
    ("DELETE", "/api/aggregates/{id}"),
}


def test_every_json_route_declares_a_response_model() -> None:
    for (method, path), route in s.ROUTE_SCHEMAS.items():
        if (method, path) in NO_RESPONSE_BODY:
            assert route.response is None
        else:
            assert route.response is not None, (method, path)
            TypeAdapter(route.response)  # a valid pydantic annotation


# ----------------------------------------------------------------------------------- errors


def test_error_response_shape_matches_what_api_js_reads() -> None:
    assert set(s.ErrorResponse.model_fields) == {"detail", "code"}
    js = API_JS.read_text()
    assert "body.detail" in js and "body.code" in js
    assert ok(s.ErrorResponse, {"detail": "nope", "code": "not_found"})


@pytest.mark.parametrize(
    "payload", [{"detail": "x"}, {"code": "x"}, {"detail": "x", "code": "Not Snake"}]
)
def test_error_response_rejects_incomplete_or_malformed(payload: dict[str, Any]) -> None:
    bad(s.ErrorResponse, payload)


# ----------------------------------------------------------------------------------- enums


def test_group_position_values_come_from_the_domain_enum() -> None:
    for position in dt_types.Position:
        assert ok(s.GroupCreate, {**GROUP, "position": position.value})
    err = bad(s.GroupCreate, {**GROUP, "position": "sandbags"})
    assert "position" in str(err)


def test_domain_enums_are_reused_not_redefined() -> None:
    assert s.GroupCreate.model_fields["position"].annotation is dt_types.Position
    assert s.GroupOut.model_fields["position"].annotation is dt_types.Position
    assert s.GroupOut.model_fields["shot_at_source"].annotation is dt_types.ShotTimeSource
    assert s.GroupCreate.model_fields["purpose"].annotation is dt_types.GroupPurpose
    assert s.ShotIn.model_fields["origin"].annotation is dt_types.ShotOrigin
    assert s.RejectionIn.model_fields["reason"].annotation is dt_types.RejectionReason
    assert s.ReferenceProposeRequest.model_fields["kind"].annotation is dt_types.ReferenceKind
    assert s.LoadCreate.model_fields["kind"].annotation is dt_types.LoadKind


# ----------------------------------------------------------------------------------- rifles/loads


def test_rifle_create_valid_and_caliber_default() -> None:
    rifle = ok(s.RifleCreate, {"name": "  Tikka  ", "cartridge": "6.5 Creedmoor"})
    assert rifle.name == "Tikka" and rifle.bullet_diameter_in is None
    assert (
        ok(
            s.RifleCreate, {"name": "a", "cartridge": "b", "bullet_diameter_in": 0.264}
        ).bullet_diameter_in
        == 0.264
    )


@pytest.mark.parametrize(
    "payload",
    [
        {"cartridge": "6.5"},
        {"name": "", "cartridge": "6.5"},
        {"name": "   ", "cartridge": "6.5"},
        {"name": "a", "cartridge": "b", "bullet_diameter_in": 0.1},
        {"name": "a", "cartridge": "b", "bullet_diameter_in": 0.6},
        {"name": "a", "cartridge": "b", "bullet_diameter_in": float("nan")},
        {"name": "a", "cartridge": "b", "surprise": 1},
    ],
)
def test_rifle_create_invalid(payload: dict[str, Any]) -> None:
    bad(s.RifleCreate, payload)


def test_patch_models_need_a_field_and_reject_null_on_required_fields() -> None:
    bad(s.RifleUpdate, {})
    bad(s.RifleUpdate, {"name": None})
    assert ok(s.RifleUpdate, {"scope": None}).model_fields_set == {"scope"}
    bad(s.GroupUpdate, {})
    for field in ("distance_yd", "position", "suppressor", "shot_date", "weather_skipped"):
        bad(s.GroupUpdate, {field: None})
    for field in ("wind_clock", "shot_time", "utc_offset", "suppressor_id", "temperature_f"):
        assert ok(s.GroupUpdate, {field: None}).model_fields_set == {field}


def test_suppressor_name_is_trimmed_and_must_be_one_to_eighty_characters() -> None:
    assert ok(s.SuppressorCreate, {"name": "  Omega 36M "}).name == "Omega 36M"
    for name in ("", "   ", "x" * 81):
        bad(s.SuppressorCreate, {"name": name})
    assert ok(s.SuppressorCreate, {"name": "x" * 80})
    bad(s.SuppressorCreate, {})
    bad(s.SuppressorCreate, {"name": "A", "id": 1})


def test_group_create_names_a_suppressor_only_when_the_suppressor_is_on() -> None:
    assert ok(s.GroupCreate, {**GROUP, "suppressor": True, "suppressor_id": 3}).suppressor_id == 3
    assert ok(s.GroupCreate, {**GROUP, "suppressor": True}).suppressor_id is None
    assert ok(s.GroupCreate, {**GROUP, "suppressor": False}).suppressor_id is None
    bad(s.GroupCreate, {**GROUP, "suppressor": False, "suppressor_id": 3})
    for value in (0, -1, 2**63):
        bad(s.GroupCreate, {**GROUP, "suppressor": True, "suppressor_id": value})


def test_group_update_allows_null_suppressor_id_but_not_with_the_suppressor_off() -> None:
    assert ok(s.GroupUpdate, {"suppressor_id": None}).model_fields_set == {"suppressor_id"}
    assert ok(s.GroupUpdate, {"suppressor": True, "suppressor_id": 2}).suppressor_id == 2
    assert ok(s.GroupUpdate, {"suppressor": False}).suppressor_id is None
    assert ok(s.GroupUpdate, {"suppressor": False, "suppressor_id": None})
    bad(s.GroupUpdate, {"suppressor": False, "suppressor_id": 2})
    bad(s.GroupUpdate, {"suppressor_id": 0})


def test_suppressor_routes_are_registered_and_group_out_carries_the_name() -> None:
    assert s.ROUTE_SCHEMAS[("POST", "/api/suppressors")].request is s.SuppressorCreate
    assert s.ROUTE_SCHEMAS[("POST", "/api/suppressors")].response is s.SuppressorOut
    assert s.ROUTE_SCHEMAS[("GET", "/api/suppressors")].request is None
    assert {"suppressor_id", "suppressor_name"} <= set(s.GroupOut.model_fields)


def test_zero_load_set_allows_null_but_not_zero_or_missing() -> None:
    assert ok(s.ZeroLoadSet, {"load_id": None}).load_id is None
    assert ok(s.ZeroLoadSet, {"load_id": 3}).load_id == 3
    bad(s.ZeroLoadSet, {"load_id": 0})
    bad(s.ZeroLoadSet, {})


def test_factory_and_handload_valid() -> None:
    assert ok(
        s.LoadCreate,
        {"kind": "factory", "label": "Fed GMM", "maker": "Federal", "bullet_weight_gr": 168},
    )
    hand = ok(
        s.LoadCreate,
        {
            "kind": "handload",
            "label": "116 gr TMK",
            "bullet_weight_gr": 116,
            "bullet": "TMK",
            "primer": "CCI BR-2",
            "brass": "Lapua",
            "coal_in": 2.8,
        },
    )
    assert (hand.bullet_weight_gr, hand.bullet, hand.brass, hand.coal_in) == (
        116,
        "TMK",
        "Lapua",
        2.8,
    )


@pytest.mark.parametrize(
    "payload",
    [
        {"kind": "factory", "label": "x", "brass": "Lapua"},
        {"kind": "factory", "label": "x", "coal_in": 2.8},
        {"kind": "factory", "label": "x", "primer": "BR-2"},
        {"kind": "factory", "label": "x", "bullet": "TMK"},
        {"kind": "handload", "label": "x", "maker": "Federal"},
        {"kind": "handload", "label": "x", "bullet_type": "BTHP"},
        {"kind": "handload", "label": "x", "powder": "H4350"},
        {"kind": "handload", "label": "x", "charge_gr": 41.0},
        {"kind": "handload", "label": "x", "cbto_in": 2.2},
        {"kind": "handload", "label": "x", "coal_in": 0.999},
        {"kind": "handload", "label": "x", "coal_in": 5.001},
        {"kind": "handload", "label": "x", "coal_in": 2.8005},
        {"kind": "rimfire", "label": "x"},
        {"kind": "factory"},
        {"kind": "factory", "label": "x", "bullet_weight_gr": 0},
        {"kind": "handload", "label": "x", "trued_mv_fps": -1},
    ],
)
def test_load_create_invalid(payload: dict[str, Any]) -> None:
    bad(s.LoadCreate, payload)


def test_load_update_forbids_changing_kind() -> None:
    bad(s.LoadUpdate, {"kind": "handload"})
    assert ok(s.LoadUpdate, {"trued_mv_fps": None})


def test_load_update_takes_the_handload_fields_and_rejects_powder_and_charge() -> None:
    update = ok(
        s.LoadUpdate, {"bullet_weight_gr": 116, "bullet": "TMK", "brass": None, "coal_in": 3.0}
    )
    assert update.model_dump(exclude_unset=True) == {
        "bullet_weight_gr": 116,
        "bullet": "TMK",
        "brass": None,
        "coal_in": 3.0,
    }
    for body in ({"powder": "H4350"}, {"charge_gr": 41.0}, {"coal_in": 5.5}, {"coal_in": 2.1234}):
        bad(s.LoadUpdate, body)


def test_load_response_has_brass_and_coal_and_no_powder_or_charge() -> None:
    fields = set(s.LoadOut.model_fields)
    assert {"bullet_weight_gr", "bullet", "primer", "brass", "coal_in"} <= fields
    assert not {"powder", "charge_gr"} & fields


def test_lot_create_valid_and_invalid() -> None:
    assert ok(s.LotCreate, {"lot_code": " A1 "}).lot_code == "A1"
    bad(s.LotCreate, {"lot_code": ""})
    bad(s.LotCreate, {"lot_code": "x" * 65})


# ----------------------------------------------------------------------------------- groups


def test_group_create_valid_with_optional_weather() -> None:
    group = ok(
        s.GroupCreate,
        {
            **GROUP,
            "temperature_f": 55,
            "wind_mph": 4,
            "wind_clock": 12,
            "density_altitude_ft": 3000,
        },
    )
    assert group.wind_clock == 12
    assert ok(s.GroupCreate, {**GROUP, "weather_skipped": True}).weather_skipped is True


@pytest.mark.parametrize(
    "override",
    [
        {"distance_yd": 0},
        {"distance_yd": -100},
        {"distance_yd": 100.5},
        {"distance_yd": 10_001},
        {"wind_clock": 0},
        {"wind_clock": 13},
        {"wind_mph": -1},
        {"temperature_f": 200},
        {"shot_date": "30/09/2026"},
        {"suppressor": "maybe"},
        {"load_id": 0},
    ],
)
def test_group_create_context_invalid(override: dict[str, Any]) -> None:
    bad(s.GroupCreate, {**GROUP, **override})


@pytest.mark.parametrize("missing", ["distance_yd", "position", "suppressor"])
def test_group_create_requires_distance_position_and_suppressor(missing: str) -> None:
    bad(s.GroupCreate, {k: v for k, v in GROUP.items() if k != missing})


def test_group_create_date_may_be_left_to_the_photo_but_a_time_needs_a_date() -> None:
    no_date = {k: v for k, v in GROUP.items() if k != "shot_date"}
    assert ok(s.GroupCreate, no_date).shot_date is None
    assert ok(s.GroupCreate, {**no_date, "photo_id": 4}).shot_time is None
    bad(s.GroupCreate, {**no_date, "shot_time": "10:15"})
    assert ok(s.GroupCreate, {**GROUP, "shot_time": "10:15"}).shot_time == "10:15:00"


@pytest.mark.parametrize(
    ("given", "stored"),
    [("09:05", "09:05:00"), ("23:59:59", "23:59:59"), ("00:00", "00:00:00"), (" 7:05 ", None)],
)
def test_shot_time_is_normalized_to_seconds(given: str, stored: str | None) -> None:
    if stored is None:
        bad(s.GroupCreate, {**GROUP, "shot_time": given})
    else:
        assert ok(s.GroupCreate, {**GROUP, "shot_time": given}).shot_time == stored


@pytest.mark.parametrize(
    "value", ["24:00", "12:60", "12:30:60", "1230", "noon", "12:30:00.5", "12:30+02:00", 5, 3600]
)
def test_shot_time_rejects_malformed_and_numeric_values(value: object) -> None:
    bad(s.GroupCreate, {**GROUP, "shot_time": value})
    bad(s.GroupUpdate, {"shot_time": value})


@pytest.mark.parametrize("value", ["-06:00", "+00:00", "+05:30", "+13:45"])
def test_utc_offset_accepts_signed_hh_mm(value: str) -> None:
    assert ok(s.GroupCreate, {**GROUP, "utc_offset": value}).utc_offset == value
    assert ok(s.GroupUpdate, {"utc_offset": value}).utc_offset == value


@pytest.mark.parametrize(
    "value", ["06:00", "-6:00", "-0600", "UTC", "+06:60", "+20:00", "+06:00:00", "", 6]
)
def test_utc_offset_rejects_anything_else(value: object) -> None:
    bad(s.GroupCreate, {**GROUP, "utc_offset": value})
    bad(s.GroupUpdate, {"utc_offset": value})


def test_entered_plus_skipped_weather_is_left_to_the_service_to_reject() -> None:
    assert ok(s.GroupCreate, {**GROUP, "temperature_f": 50, "weather_skipped": True})
    assert ok(s.GroupUpdate, {"wind_clock": 3, "weather_skipped": True})
    assert ok(s.GroupUpdate, {"weather_skipped": True, "temperature_f": None})


def test_purpose_and_zero_id_on_create_are_left_to_the_service_to_refuse() -> None:
    assert ok(s.GroupCreate, {**GROUP, "purpose": "general", "zero_id": None})
    assert ok(s.GroupCreate, {**GROUP, "purpose": "zero_step1", "zero_id": 3}).zero_id == 3
    bad(s.GroupCreate, {**GROUP, "purpose": "warmup"})


def test_group_has_no_session_fields_anywhere() -> None:
    for model in (s.GroupCreate, s.GroupUpdate, s.GroupOut, s.GroupDetailOut, s.GroupListQuery):
        assert "session_id" not in model.model_fields, model.__name__
    assert not any(name.startswith("Session") for name in dir(s))
    assert "session" not in s.SearchKind.__args__
    for path in (("GET", "/api/sessions"), ("POST", "/api/sessions/{id}/groups")):
        assert path not in s.ROUTE_SCHEMAS


def test_group_defaults_are_all_optional_and_describe_a_previous_group() -> None:
    empty = ok(s.GroupDefaultsOut, {})
    assert all(value is None for value in empty.model_dump().values())
    expected = {
        "source_group_id", "load_id", "lot_code", "shots_expected", "shot_date", "distance_yd",
        "position", "suppressor", "suppressor_id", "temperature_f", "density_altitude_ft",
        "wind_mph", "wind_clock", "weather_skipped",
    }  # fmt: skip
    assert set(s.GroupDefaultsOut.model_fields) == expected
    route = s.ROUTE_SCHEMAS[("GET", "/api/rifles/{id}/group-defaults")]
    assert (route.request, route.query, route.response) == (None, None, s.GroupDefaultsOut)


def test_group_create_route_takes_group_create_and_returns_group_out() -> None:
    route = s.ROUTE_SCHEMAS[("POST", "/api/groups")]
    assert (route.request, route.response) == (s.GroupCreate, s.GroupOut)


def test_group_create_carries_lot_code_and_defaults() -> None:
    group = ok(s.GroupCreate, GROUP)
    assert group.lot_code == "LOT-7"
    assert group.purpose is dt_types.GroupPurpose.GENERAL
    assert group.zero_id is None and group.elevation_dialed_mil is None


@pytest.mark.parametrize(
    "override",
    [
        {"shots_expected": 0},
        {"shots_expected": -3},
        {"shots_expected": 201},
        {"lot_code": ""},
        {"purpose": "warmup"},
        {"load_id": 0},
    ],
)
def test_group_create_invalid(override: dict[str, Any]) -> None:
    bad(s.GroupCreate, {**GROUP, **override})


def test_group_create_requires_lot_code() -> None:
    bad(s.GroupCreate, {"load_id": 1, "shots_expected": 5})


def test_group_list_query_filters_are_optional_and_typed() -> None:
    assert ok(s.GroupListQuery, {}).model_dump() == {
        "load_id": None,
        "rifle_id": None,
        "status": None,
    }
    query = ok(s.GroupListQuery, {"status": "final", "rifle_id": 1, "load_id": 3})
    assert (query.rifle_id, query.load_id) == (1, 3)
    assert query.status is dt_types.GroupStatus.FINAL


@pytest.mark.parametrize(
    "payload",
    [{"status": "archived"}, {"session_id": 1}, {"rifle_id": "abc"}, {"owner": 1}],
)
def test_group_list_query_invalid(payload: dict[str, Any]) -> None:
    bad(s.GroupListQuery, payload)


def test_group_list_route_returns_a_bare_array_of_group_items() -> None:
    route = s.ROUTE_SCHEMAS[("GET", "/api/groups")]
    assert route.query is s.GroupListQuery and route.response == list[s.GroupOut]


def test_group_update_can_link_photo_and_clear_elevation() -> None:
    assert ok(s.GroupUpdate, {"photo_id": 4}).photo_id == 4
    assert ok(s.GroupUpdate, {"elevation_dialed_mil": None})
    bad(s.GroupUpdate, {"shots_expected": 0})


def test_group_update_takes_the_context_fields() -> None:
    update = ok(
        s.GroupUpdate,
        {
            "shot_date": "2026-10-01",
            "shot_time": "08:30",
            "utc_offset": "-06:00",
            "distance_yd": 300,
            "position": "prone_off_pack",
            "suppressor": True,
            "suppressor_id": 2,
            "temperature_f": 48.5,
            "wind_clock": 9,
            "weather_skipped": False,
        },
    )
    assert update.shot_time == "08:30:00" and update.position is dt_types.Position.PRONE_OFF_PACK
    for body in ({"distance_yd": 0}, {"position": "sandbags"}, {"shot_date": "yesterday"}):
        bad(s.GroupUpdate, body)


# ----------------------------------------------------------------------------------- photos/reference


def test_reference_propose_accepts_each_kind_and_rejects_others() -> None:
    for kind in dt_types.ReferenceKind:
        assert ok(s.ReferenceProposeRequest, {"kind": kind.value})
    bad(s.ReferenceProposeRequest, {"kind": "circle_4in"})


def test_reference_confirm_by_candidate_index() -> None:
    assert ok(s.ReferenceConfirmRequest, {"kind": "circle_3in", "candidate_index": 0})


def test_reference_confirm_manual_circle_and_grid() -> None:
    assert ok(s.ReferenceConfirmRequest, {"kind": "circle_1in", "manual": CIRCLE_MANUAL})
    assert ok(s.ReferenceConfirmRequest, {"kind": "grid_1in", "manual": GRID_MANUAL})


@pytest.mark.parametrize(
    "payload",
    [
        {"kind": "circle_1in"},
        {"kind": "circle_1in", "candidate_index": 0, "manual": CIRCLE_MANUAL},
        {"kind": "circle_1in", "candidate_index": -1},
        {"kind": "grid_1in", "manual": CIRCLE_MANUAL},
        {"kind": "circle_3in", "manual": GRID_MANUAL},
        {"kind": "circle_3in", "manual": {"edge_points_px": CIRCLE_MANUAL["edge_points_px"][:2]}},
        {"kind": "circle_3in", "manual": {}},
        {"kind": "grid_1in", "manual": {"grid": {**GRID_MANUAL["grid"], "corners_px": []}}},
        {"kind": "grid_1in", "manual": {"grid": {**GRID_MANUAL["grid"], "squares_x": 0}}},
    ],
)
def test_reference_confirm_invalid(payload: dict[str, Any]) -> None:
    bad(s.ReferenceConfirmRequest, payload)


def test_snap_aim_valid_and_invalid() -> None:
    assert ok(s.SnapAimRequest, {"x_px": 10.5, "y_px": 20})
    bad(s.SnapAimRequest, {"x_px": -1, "y_px": 2})
    bad(s.SnapAimRequest, {"x_px": 1})


def test_image_query_defaults_and_rejects_unknown_variant() -> None:
    assert s.ImageQuery.model_validate({}).variant is s.ImageVariant.ORIENTED
    assert ok(s.ImageQuery, {"variant": "working"})
    bad(s.ImageQuery, {"variant": "original"})


# ----------------------------------------------------------------------------------- detect/review


def test_detect_request_roi_optional_and_validated() -> None:
    assert ok(s.DetectRequest, {}).roi_px is None
    assert ok(s.DetectRequest, {"roi_px": {"x0": 0, "y0": 0, "x1": 10, "y1": 10}})
    bad(s.DetectRequest, {"roi_px": {"x0": 10, "y0": 0, "x1": 10, "y1": 10}})
    bad(s.DetectRequest, {"roi_px": {"x0": 0, "y0": 5, "x1": 10, "y1": 4}})
    bad(s.DetectRequest, {"roi_px": {"x0": -1, "y0": 0, "x1": 10, "y1": 10}})


def test_review_request_valid() -> None:
    review = ok(s.ReviewRequest, REVIEW)
    assert review.shots[0].origin is dt_types.ShotOrigin.DETECTED
    assert ok(s.ReviewRequest, {**REVIEW, "aim": None})
    assert ok(
        s.ReviewRequest,
        {**REVIEW, "rejections": [{"x_in": 1, "y_in": 1, "reason": "not_a_bullet_hole"}]},
    )


def test_review_rejection_reason_flier_is_rejected() -> None:
    payload = {**REVIEW, "rejections": [{"x_in": 1, "y_in": 1, "reason": "flier"}]}
    err = bad(s.ReviewRequest, payload)
    assert "reason" in str(err)


@pytest.mark.parametrize("reason", ["flier", "miss", "", "NOT_A_BULLET_HOLE", None])
def test_rejection_reason_only_accepts_not_a_bullet_hole(reason: Any) -> None:
    bad(s.RejectionIn, {"x_in": 0, "y_in": 0, "reason": reason})


def test_rejection_reason_must_be_explicit() -> None:
    bad(s.RejectionIn, {"x_in": 0, "y_in": 0})
    assert ok(s.RejectionIn, {"x_in": 0, "y_in": 0, "reason": "not_a_bullet_hole"})


@pytest.mark.parametrize(
    "override",
    [
        {"shots_expected": 0},
        {"shots_expected": -1},
        {"shots": [{"x_in": 1, "y_in": 1, "origin": "guess"}]},
        {"shots": [{"x_in": float("inf"), "y_in": 1, "origin": "manual"}]},
        {"shots": [{"x_in": 1, "y_in": 1, "origin": "manual", "confidence": 1.5}]},
        {"aim": {"x_in": 0}},
        {"rejections": [{"x_in": 1, "y_in": 1, "reason": "not_a_bullet_hole", "why": "x"}]},
    ],
)
def test_review_request_invalid(override: dict[str, Any]) -> None:
    bad(s.ReviewRequest, {**REVIEW, **override})


def test_review_request_requires_shots_and_shots_expected() -> None:
    bad(s.ReviewRequest, {"shots_expected": 1})
    bad(s.ReviewRequest, {"shots": []})


def test_review_request_allows_empty_shot_list_for_a_fresh_draft() -> None:
    assert ok(s.ReviewRequest, {"shots": [], "shots_expected": 5}).shots == []


# ----------------------------------------------------------------------------------- aggregate/search


def test_aggregate_request() -> None:
    assert ok(s.AggregateRequest, {"group_ids": [1, 2, 3]})
    bad(s.AggregateRequest, {"group_ids": []})
    bad(s.AggregateRequest, {"group_ids": [1, 1]})
    bad(s.AggregateRequest, {"group_ids": [0]})
    bad(s.AggregateRequest, {})


def test_search_query() -> None:
    assert ok(s.SearchQuery, {"q": "100%_x"}).q == "100%_x"
    bad(s.SearchQuery, {"q": ""})
    bad(s.SearchQuery, {"q": "   "})
    bad(s.SearchQuery, {})


# ----------------------------------------------------------------------------------- comparison


def test_comparison_query_parses_csv_list_and_filters() -> None:
    query = ok(s.ComparisonQuery, {"load_ids": "1, 2,3", "position": "bench", "suppressor": "true"})
    assert query.load_ids == [1, 2, 3]
    assert query.position is dt_types.Position.BENCH and query.suppressor is True
    assert ok(s.ComparisonQuery, {"load_ids": [4]}).position is None


@pytest.mark.parametrize(
    "payload",
    [
        {"load_ids": ""},
        {"load_ids": "1,x"},
        {"load_ids": "1,1"},
        {"load_ids": "0"},
        {"load_ids": "1", "position": "sandbags"},
        {},
    ],
)
def test_comparison_query_invalid(payload: dict[str, Any]) -> None:
    bad(s.ComparisonQuery, payload)


def _tier(rounds: int = 0, groups: int = 0, mean: float | None = None) -> dict[str, Any]:
    return {"rounds": rounds, "groups": groups, "mean_es_moa": mean, "worst_es_moa": mean}


def test_comparison_response_carries_tier10_and_tier30_summaries() -> None:
    load = {
        "load_id": 1,
        "label": "A",
        "is_zero_load": True,
        "tier10": _tier(20, 2, 1.1),
        "tier30": _tier(),
        "status": "high_confidence",
        "precision_verdict": "sufficient for deer/elk to 800 yd",
        "poi_shift": None,
        "poi_shift_note": "shoot both loads on the same day, distance, position and suppressor setting to measure POI shift",
        "groups": [
            {
                "group_id": 5,
                "shot_date": "2026-09-30",
                "shot_time": None,
                "distance_yd": 100,
                "position": "bench",
                "suppressor": False,
                "shots": 25,
                "es_moa": 1.4,
                "includes_estimated": False,
                "tier": None,
            }
        ],
        "aggregates": [
            {
                "aggregate_id": 3,
                "name": "Three of ten",
                "group_ids": [1, 2, 3],
                "distance_yd": 100,
                "shots": 30,
                "es_moa": 1.6,
                "includes_estimated": False,
                "tier": "tier30",
                "counted": True,
                "note": None,
            }
        ],
    }
    body = {
        "rifle_id": 1,
        "filters": {"position": None, "suppressor": None},
        "loads": [load],
        "pairs": [
            {
                "a_load_id": 1,
                "b_load_id": 2,
                "tier10": "no_real_difference",
                "tier30": "insufficient_data",
            }
        ],
        "conditions_warning": None,
        "zero_to_hunting_load_advice": "zero the rifle to your hunting load",
    }
    out = ok(s.ComparisonOut, body)
    assert out.loads[0].tier10.rounds == 20 and out.loads[0].tier30.mean_es_moa is None
    assert out.loads[0].aggregates[0].counted is True
    assert out.pairs[0].tier30 == "insufficient_data"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda b: b["loads"][0].pop("tier30"),
        lambda b: b["loads"][0].update(status="great"),
        lambda b: b["loads"][0]["groups"][0].update(tier="tier20"),
        lambda b: b["pairs"][0].update(tier10="a_wins"),
        lambda b: b["loads"][0].pop("aggregates"),
    ],
)
def test_comparison_response_invalid(mutate: Any) -> None:
    body = {
        "rifle_id": 1,
        "filters": {"position": None, "suppressor": None},
        "loads": [
            {
                "load_id": 1,
                "label": "A",
                "is_zero_load": False,
                "tier10": _tier(),
                "tier30": _tier(),
                "status": "insufficient_data",
                "precision_verdict": None,
                "poi_shift": None,
                "poi_shift_note": None,
                "groups": [
                    {
                        "group_id": 5,
                        "shot_date": "2026-09-30",
                        "shot_time": None,
                        "distance_yd": 100,
                        "position": "bench",
                        "suppressor": False,
                        "shots": 10,
                        "es_moa": 1.0,
                        "includes_estimated": False,
                        "tier": "tier10",
                    }
                ],
                "aggregates": [],
            }
        ],
        "pairs": [
            {
                "a_load_id": 1,
                "b_load_id": 2,
                "tier10": "insufficient_data",
                "tier30": "insufficient_data",
            }
        ],
        "conditions_warning": None,
        "zero_to_hunting_load_advice": "x",
    }
    mutate(body)
    bad(s.ComparisonOut, body)


# ----------------------------------------------------------------------------------- zeroing


def test_zero_create_valid_and_invalid() -> None:
    assert ok(s.ZeroCreate, {"rifle_id": 1, "load_id": 2, "mode": "new_setup"})
    assert ok(s.ZeroCreate, {"rifle_id": 1, "load_id": 2, "mode": "existing", "lot_code": "L1"})
    bad(s.ZeroCreate, {"rifle_id": 1, "load_id": 2, "mode": "rezero"})
    bad(s.ZeroCreate, {"rifle_id": 1, "mode": "existing"})


def test_zero_link_group_and_check_reference_a_group() -> None:
    assert ok(s.ZeroLinkGroup, {"group_id": 9}).group_id == 9
    assert ok(s.ZeroCheckCreate, {"group_id": 9}).group_id == 9
    bad(s.ZeroCheckCreate, {})
    bad(s.ZeroCheckCreate, {"group_id": 0})
    bad(s.ZeroLinkGroup, {"group_id": "abc"})


@pytest.mark.parametrize(
    "payload", [{"up_mil": -0.2, "right_mil": 0.3}, {"up_mil": 0, "right_mil": 0}]
)
def test_zero_dialed_valid(payload: dict[str, Any]) -> None:
    assert ok(s.ZeroDialed, payload)


@pytest.mark.parametrize(
    "payload",
    [
        {"up_mil": 0.25, "right_mil": 0.1},
        {"up_mil": 0.1},
        {"up_mil": 500, "right_mil": 0},
        {"up_mil": float("nan"), "right_mil": 0},
    ],
)
def test_zero_dialed_invalid(payload: dict[str, Any]) -> None:
    bad(s.ZeroDialed, payload)


def test_zero_dot_valid_and_invalid() -> None:
    assert ok(s.ZeroDot, {"dot_size_in": 1.25})
    bad(s.ZeroDot, {"dot_size_in": 0})
    bad(s.ZeroDot, {"dot_size_in": -1})
    bad(s.ZeroDot, {"dot_size_in": 11})


FULL_STATS = {
    "n": 4,
    "es_in": 1.0,
    "es_moa": 0.95,
    "mean_dx_in": 0.1,
    "mean_dy_in": -0.1,
    "mean_dx_mil": 0.03,
    "mean_dy_mil": -0.03,
    "includes_estimated": False,
    "poi_text": "0.3 mil high, 0.2 mil right",
}
SIZE_ONLY_STATS = {"n": 4, "es_in": 1.0, "es_moa": 0.95, "includes_estimated": False}
NO_NUMBER_KINDS = ("shoot_more", "invalid")


def test_advice_never_carries_a_correction_for_shoot_more_or_invalid() -> None:
    correction = {"up_mil": -0.2, "right_mil": 0.3, "text": "Dial DOWN 0.2, RIGHT 0.3"}
    assert ok(s.AdviceOut, {"kind": "shoot_more", "message": "Shoot more"}).correction is None
    for kind in NO_NUMBER_KINDS:
        bad(s.AdviceOut, {"kind": kind, "message": "x", "correction": correction})
    assert ok(s.AdviceOut, {"kind": "correction", "message": "x", "correction": correction})
    bad(s.AdviceOut, {"kind": "correction", "message": "x"})
    assert ok(s.AdviceOut, {"kind": "no_adjustment", "message": "x"})


@pytest.mark.parametrize("kind", NO_NUMBER_KINDS)
def test_short_group_advice_rejects_full_stats_with_mil_poi(kind: str) -> None:
    err = bad(s.AdviceOut, {"kind": kind, "message": "Shoot more", "stats": FULL_STATS})
    assert "mean POI" in str(err)


@pytest.mark.parametrize("kind", NO_NUMBER_KINDS)
@pytest.mark.parametrize(
    "leak",
    [
        {"mean_dx_mil": 0.3},
        {"mean_dy_mil": -0.2},
        {"mean_dx_in": 0.1},
        {"poi_text": "0.3 mil high, 0.2 mil right"},
        {"mean_dx_mil": 0.3, "poi_text": "0.3 mil right"},
        {"up_mil": -0.2},
        {"direction": "LEFT"},
    ],
)
def test_short_group_advice_rejects_any_partial_poi_field_in_stats(
    kind: str, leak: dict[str, Any]
) -> None:
    bad(s.AdviceOut, {"kind": kind, "message": "Shoot more", "stats": {**SIZE_ONLY_STATS, **leak}})


# Each negative is flagged by exactly one pattern, so dropping any pattern fails its own case.
CORRECTION_TEXT_NEGATIVES = [
    ("decimal number", "Shoot more; off by 0.3"),
    ("decimal number", "Shoot more, about .5 away"),
    ("unit or dial wording", "Dialed in already"),
    ("unit or dial wording", "Dial it"),
    ("unit or dial wording", "adjust 2 clicks"),
    ("unit or dial wording", "Clicked over"),
    ("unit or dial wording", "check your mil"),
    ("unit or dial wording", "off by 2 mrad"),
    ("unit or dial wording", "off by 2 MOA"),
    ("come up/down", "Come up a bit"),
    ("come up/down", "come   down a bit"),
    ("direction before number", "Correct UP 2"),
    ("direction before number", "left 3"),
    ("direction before number", "Hold RIGHT: 1"),
    ("number before direction", "2 down"),
    ("number before direction", "3 high"),
    ("number before direction", "1 - low"),
]
REVIEW_NEGATIVES = [
    "DOWN 0.2, LEFT 0.3",
    "Correct UP 0.2 RIGHT 0.1",
    "Group is 0.3 high and 0.2 right; shoot more",
    "Come up 0.2 mrad",
    "0.4 MOA low",
    "Dialed DOWN 0.2",
    "Dial DOWN 0.2, LEFT 0.3",
    "group is 0.3 mil high",
]
PLAIN_MESSAGES = [
    "Shoot more shots: 4 of 10 so far, 6 left to fire",
    "Shoot more: 4 of 10 shots so far",
    "Need at least 10 shots; you have 4",
    "shots left 6",
    "rounds left: 6",
    "No shots found in this group",
    "Group needs at least 1 shot",
]


@pytest.mark.parametrize("kind", NO_NUMBER_KINDS)
@pytest.mark.parametrize(
    ("reason", "message"), CORRECTION_TEXT_NEGATIVES, ids=lambda v: str(v)[:40]
)
def test_short_group_advice_rejects_each_correction_text_pattern(
    kind: str, reason: str, message: str
) -> None:
    assert s._correction_text_reason(message) == reason
    err = bad(s.AdviceOut, {"kind": kind, "message": message})
    assert reason in str(err)


@pytest.mark.parametrize("kind", NO_NUMBER_KINDS)
@pytest.mark.parametrize("message", REVIEW_NEGATIVES)
def test_short_group_advice_rejects_the_review_examples(kind: str, message: str) -> None:
    bad(s.AdviceOut, {"kind": kind, "message": message})


@pytest.mark.parametrize("kind", NO_NUMBER_KINDS)
@pytest.mark.parametrize("message", PLAIN_MESSAGES)
def test_short_group_advice_accepts_plain_count_messages(kind: str, message: str) -> None:
    assert s._correction_text_reason(message) is None
    assert ok(s.AdviceOut, {"kind": kind, "message": message})


def test_every_correction_text_pattern_has_a_negative_example() -> None:
    assert {reason for reason, _ in CORRECTION_TEXT_NEGATIVES} == set(s._CORRECTION_TEXT_PATTERNS)


@pytest.mark.parametrize(
    "kind", ["correction", "touch_up_ok", "no_adjustment", "confirm_again", "ready_for_dot"]
)
def test_message_text_is_unrestricted_for_kinds_that_may_carry_a_number(kind: str) -> None:
    correction = {"up_mil": -0.2, "right_mil": 0.3, "text": "Dial DOWN 0.2, RIGHT 0.3"}
    payload: dict[str, Any] = {"kind": kind, "message": "Dial DOWN 0.2, LEFT 0.3"}
    if kind == "correction":
        payload["correction"] = correction
    assert ok(s.AdviceOut, payload)


@pytest.mark.parametrize("kind", NO_NUMBER_KINDS)
def test_short_group_advice_allows_size_only_stats_and_plain_message(kind: str) -> None:
    advice = ok(
        s.AdviceOut,
        {
            "kind": kind,
            "message": "Shoot more shots: 4 of 10 so far, 6 left to fire",
            "stats": SIZE_ONLY_STATS,
        },
    )
    dumped = advice.model_dump(mode="json")
    assert set(dumped["stats"]) == {"n", "es_in", "es_moa", "includes_estimated", "es_label"}
    assert dumped["correction"] is None


def test_short_group_advice_from_domain_dataclass_with_full_stats_is_rejected() -> None:
    stats = dt_types.GroupStats(4, 1.0, 0.95, 0.1, -0.1, 0.03, -0.03, False)
    advice = dt_types.Advice(dt_types.AdviceKind.SHOOT_MORE, "Shoot more", None, stats)
    with pytest.raises(ValidationError):
        s.AdviceOut.model_validate(advice)


def test_full_stats_remain_allowed_where_a_number_is_legitimate() -> None:
    assert ok(
        s.AdviceOut,
        {"kind": "touch_up_ok", "message": "0.1 mil touch-up is fine", "stats": FULL_STATS},
    )
    assert ok(s.AdviceOut, {"kind": "confirm_again", "message": "x", "stats": SIZE_ONLY_STATS})


def _stats_route_adapter() -> TypeAdapter[Any]:
    return TypeAdapter(s.ROUTE_SCHEMAS[("GET", "/api/groups/{id}/stats")].response)


def test_stats_route_accepts_size_only_body() -> None:
    parsed = _stats_route_adapter().validate_python(SIZE_ONLY_STATS)
    assert isinstance(parsed, s.SizeOnlyStatsOut)
    assert set(parsed.model_dump()) == {"n", "es_in", "es_moa", "includes_estimated", "es_label"}


def test_stats_route_still_accepts_full_body() -> None:
    parsed = _stats_route_adapter().validate_python(FULL_STATS)
    assert isinstance(parsed, s.GroupStatsOut)
    assert parsed.mean_dx_mil == 0.03


@pytest.mark.parametrize(
    "mixed",
    [
        {**SIZE_ONLY_STATS, "mean_dx_mil": 0.1},
        {**SIZE_ONLY_STATS, "poi_text": "0.3 mil high"},
        {**SIZE_ONLY_STATS, "mean_dx_in": 0.1, "mean_dy_in": 0.1},
        {k: v for k, v in FULL_STATS.items() if k != "mean_dy_mil"},
    ],
)
def test_stats_route_rejects_mixed_bodies(mixed: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        _stats_route_adapter().validate_python(mixed)


def test_group_detail_carries_no_stats_field() -> None:
    assert "stats" not in s.GroupDetailOut.model_fields


def test_size_only_stats_forbids_extra_fields() -> None:
    bad(s.SizeOnlyStatsOut, {**SIZE_ONLY_STATS, "mean_dx_mil": 0.1})
    assert set(s.SizeOnlyStatsOut.model_fields) == {
        "n",
        "es_in",
        "es_moa",
        "includes_estimated",
        "es_label",
    }


# ----------------------------------------------------------------------------------- responses from rows


def test_response_models_build_from_db_row_dataclasses() -> None:
    rifle = rows.RifleRow(1, "r1", "Tikka", "6.5 Creedmoor", 0.264, None, "", None, "t", "t")
    assert s.RifleOut.model_validate(rifle).name == "Tikka"
    shot = rows.ShotRow(1, 1, 1, 1.0, 2.0, None, None, None, None, "manual", 1, None)
    assert s.ShotOut.model_validate(shot).moved_by_user is True
    rejection = rows.RejectionRow(1, 1, 1.0, 1.0, "not_a_bullet_hole", "t")
    assert (
        s.RejectionOut.model_validate(rejection).reason
        is dt_types.RejectionReason.NOT_A_BULLET_HOLE
    )
    load = rows.LoadRow(
        1,
        "l1",
        1,
        "factory",
        "x",
        "Fed",
        "GMM",
        168.0,
        "BTHP",
        None,
        None,
        None,
        "",
        "t",
        "t",
        None,
        None,
    )
    assert s.LoadOut.model_validate(load).kind is dt_types.LoadKind.FACTORY


def test_response_models_reject_unknown_enum_values_from_storage() -> None:
    for field, value in (("position", "sandbags"), ("shot_at_source", "guess")):
        with pytest.raises(ValidationError):
            s.GroupOut.model_validate({**GROUP_OUT, field: value})


def test_advice_and_stats_build_from_domain_dataclasses() -> None:
    stats = dt_types.GroupStats(10, 1.0, 0.95, 0.1, -0.1, 0.03, -0.03, False)
    advice = dt_types.Advice(
        dt_types.AdviceKind.CORRECTION,
        "Dial DOWN 0.2",
        dt_types.Correction(-0.2, 0.0, "Dial DOWN 0.2"),
        stats,
    )
    out = s.AdviceOut.model_validate(advice)
    assert out.correction is not None and out.correction.up_mil == -0.2
    assert out.stats is not None and out.stats.es_moa == 0.95
    assert out.stats.es_label == s.ES_LABEL


def test_shoot_more_advice_from_domain_serializes_without_a_correction_number() -> None:
    out = s.AdviceOut.model_validate(dt_types.Advice(dt_types.AdviceKind.SHOOT_MORE, "Shoot more"))
    assert out.model_dump(mode="json")["correction"] is None


GROUP_OUT = {
    "id": 1,
    "public_id": "g1",
    "rifle_id": 1,
    "load_id": 1,
    "lot_id": 1,
    "lot_code": "L",
    "photo_id": None,
    "roi_px": {"x0": 0, "y0": 0, "x1": 5, "y1": 5},
    "shot_date": "2026-09-30",
    "shot_time": "08:15:00",
    "utc_offset": "-06:00",
    "shot_at_source": "photo",
    "distance_yd": 100,
    "position": "bench",
    "suppressor": True,
    "suppressor_id": 2,
    "suppressor_name": "Omega",
    "temperature_f": None,
    "density_altitude_ft": None,
    "wind_mph": None,
    "wind_clock": None,
    "weather_skipped": False,
    "shots_expected": 10,
    "elevation_dialed_mil": None,
    "aim": {"x_in": 1.0, "y_in": 2.0},
    "purpose": "general",
    "zero_id": None,
    "status": "draft",
    "notes": "",
    "finalized_at": None,
    "created_at": "t",
    "updated_at": "t",
}
GROUP_DETAIL = {
    **GROUP_OUT,
    "shots": [{"seq": 1, "x_in": 1, "y_in": 1, "origin": "estimated"}],
    "rejections": [],
    "frame": {"ppi": 200.0, "width_px": 100, "height_px": 100},
    "bullet_diameter_in": 0.264,
    "load_label": "A",
}


def test_error_free_json_serialization_of_a_group_with_shots() -> None:
    dumped = s.GroupDetailOut.model_validate(GROUP_DETAIL).model_dump(mode="json")
    assert dumped["new_lot"] is False and dumped["shots"][0]["origin"] == "estimated"
    assert dumped["shot_time"] == "08:15:00" and dumped["shot_at_source"] == "photo"
    assert dumped["shot_date"] == "2026-09-30" and dumped["suppressor_name"] == "Omega"
    assert dumped["legacy_session_notes"] == "" and dumped["rifle_id"] == 1


def test_group_out_context_fields_and_detail_inheritance() -> None:
    context = {
        "rifle_id", "shot_date", "shot_time", "utc_offset", "shot_at_source", "distance_yd",
        "position", "suppressor", "suppressor_id", "suppressor_name", "temperature_f",
        "density_altitude_ft", "wind_mph", "wind_clock", "weather_skipped", "weather_prompt",
    }  # fmt: skip
    assert context <= set(s.GroupOut.model_fields)
    assert context <= set(s.GroupDetailOut.model_fields)
    assert "distance_yd" not in s.GroupDetailOut.__annotations__
    assert "legacy_session_notes" in s.GroupDetailOut.model_fields
    assert "legacy_session_notes" not in s.GroupOut.model_fields


def test_group_out_needs_a_date_a_source_and_a_distance_but_not_the_optional_context() -> None:
    minimal = {
        k: v for k, v in GROUP_OUT.items()
        if k not in {"shot_time", "utc_offset", "suppressor_id", "suppressor_name", "temperature_f",
                     "density_altitude_ft", "wind_mph", "wind_clock", "weather_skipped"}
    }  # fmt: skip
    out = s.GroupOut.model_validate(minimal)
    assert (out.shot_time, out.suppressor_name, out.weather_skipped) == (None, None, False)
    for required in (
        "shot_date",
        "shot_at_source",
        "distance_yd",
        "position",
        "suppressor",
        "rifle_id",
    ):
        with pytest.raises(ValidationError):
            s.GroupOut.model_validate({k: v for k, v in minimal.items() if k != required})


def test_group_out_has_weather_prompt_defaulting_to_false_and_accepting_true() -> None:
    assert s.GroupOut.model_validate(GROUP_OUT).weather_prompt is False
    out = s.GroupOut.model_validate({**GROUP_OUT, "distance_yd": 300, "weather_prompt": True})
    assert out.weather_prompt is True
    detail = s.GroupDetailOut.model_validate({**GROUP_DETAIL, "weather_prompt": True})
    assert detail.model_dump()["weather_prompt"] is True


def test_photo_out_carries_the_capture_time_and_defaults_to_none() -> None:
    photo = {
        "id": 1, "sha256": "a" * 64, "ext": "jpg", "width_px": 10, "height_px": 10,
        "reference_kind": None, "reference_confirmed": False, "working_ppi": None,
        "working_w": None, "working_h": None, "axis_ratio": None, "created_at": "t",
    }  # fmt: skip
    plain = s.PhotoOut.model_validate(photo)
    assert (plain.taken_at, plain.taken_at_offset) == (None, None)
    dated = s.PhotoOut.model_validate(
        {**photo, "taken_at": "2026-09-30T08:15:00", "taken_at_offset": "-06:00"}
    )
    assert (dated.taken_at, dated.taken_at_offset) == ("2026-09-30T08:15:00", "-06:00")


def test_compare_and_dope_rows_carry_shot_date_and_time_not_session() -> None:
    for model in (s.CompareGroupOut, s.DopeRowOut):
        names = set(model.model_fields)
        assert {"shot_date", "shot_time"} <= names and not {"session_id", "date"} & names
        assert model.model_fields["shot_time"].default is None


def test_check_group_create_body_has_context_and_no_session() -> None:
    body = {"lot_code": "L1", "shots_expected": 2, "position": "bench", "suppressor": False}
    out = ok(s.ZeroCheckGroupCreate, body)
    assert (out.shot_date, out.shot_time, out.photo_id, out.notes) == (None, None, None, "")
    full = ok(
        s.ZeroCheckGroupCreate,
        {**body, "shot_date": "2026-10-01", "shot_time": "9:05".replace("9:", "09:"),
         "suppressor": True, "suppressor_id": 4, "photo_id": 7, "notes": "n"},
    )  # fmt: skip
    assert full.shot_time == "09:05:00" and full.suppressor_id == 4 and full.photo_id == 7
    for extra in ({"session_id": 1}, {"distance_yd": 200}, {"purpose": "zero_check"}):
        bad(s.ZeroCheckGroupCreate, {**body, **extra})
    for missing in ("lot_code", "shots_expected", "position", "suppressor"):
        bad(s.ZeroCheckGroupCreate, {k: v for k, v in body.items() if k != missing})
    bad(s.ZeroCheckGroupCreate, {**body, "shots_expected": 3})
    bad(s.ZeroCheckGroupCreate, {**body, "suppressor": False, "suppressor_id": 1})
    bad(s.ZeroCheckGroupCreate, {**body, "shot_time": "09:05"})


SAVE_AGGREGATE = {"name": "Three of ten", "group_ids": [1, 2, 3]}


def test_save_aggregate_request_accepts_a_name_and_at_least_two_unique_groups() -> None:
    out = ok(s.SaveAggregateRequest, SAVE_AGGREGATE)
    assert out.group_ids == [1, 2, 3] and out.name == "Three of ten"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda b: b.pop("name"),
        lambda b: b.update(name="  "),
        lambda b: b.update(group_ids=[1]),
        lambda b: b.update(group_ids=[1, 1]),
        lambda b: b.update(group_ids=[0, 1]),
        lambda b: b.update(group_ids=[1, 2**63]),
        lambda b: b.update(group_ids=list(range(1, 102))),
        lambda b: b.update(extra=1),
    ],
)
def test_save_aggregate_request_invalid(mutate: Any) -> None:
    body = {**SAVE_AGGREGATE, "group_ids": list(SAVE_AGGREGATE["group_ids"])}
    mutate(body)
    bad(s.SaveAggregateRequest, body)


def test_aggregate_list_query_bounds_the_load_id() -> None:
    assert ok(s.AggregateListQuery, {}).load_id is None
    assert ok(s.AggregateListQuery, {"load_id": 2**63 - 1}).load_id == 2**63 - 1
    bad(s.AggregateListQuery, {"load_id": 0})
    bad(s.AggregateListQuery, {"load_id": 2**63})


def test_share_query_knows_the_numbers_layer_and_defaults_to_photo_spread_numbers() -> None:
    assert s.ShareQuery().layers == ["photo", "spread", "numbers"]
    assert s.SHARE_DEFAULT_LAYERS == ("photo", "spread", "numbers")
    every = "photo,spread,thing_hitter,poi,correction,numbers"
    assert ok(s.ShareQuery, {"layers": every}).layers == every.split(",")
    assert ok(s.ShareQuery, {"layers": "numbers"}).layers == ["numbers"]
    assert ok(s.ShareQuery, {"layers": ""}).layers == []
    with pytest.raises(ValidationError):
        s.ShareQuery.model_validate({"layers": "number"})


# ----------------------------------------------------------------------------------- settings

SHARE_DEFAULTS = {
    "photo": True,
    "spread": True,
    "thing_hitter": False,
    "poi": False,
    "correction": False,
    "numbers": True,
}


def test_share_defaults_take_exactly_the_six_layers_as_booleans() -> None:
    assert ok(s.ShareDefaults, SHARE_DEFAULTS).model_dump() == SHARE_DEFAULTS
    assert ok(s.ShareDefaultsOut, SHARE_DEFAULTS).model_dump() == SHARE_DEFAULTS
    for name in SHARE_DEFAULTS:
        bad(s.ShareDefaults, {k: v for k, v in SHARE_DEFAULTS.items() if k != name})
        for value in (1, 0, "true", "yes", None, [True]):
            bad(s.ShareDefaults, {**SHARE_DEFAULTS, name: value})
    bad(s.ShareDefaults, {**SHARE_DEFAULTS, "extra": True})
    bad(s.ShareDefaults, {})


def test_share_defaults_cover_the_share_route_layers_and_the_service_built_ins() -> None:
    from rga.services import settings as service

    layers = set(s.ShareLayer.__args__)
    assert set(s.ShareDefaults.model_fields) == set(s.ShareDefaultsOut.model_fields) == layers
    assert set(service.SHARE_LAYERS) == layers
    assert {k for k, v in service.BUILT_IN_SHARE_DEFAULTS.items() if v} == set(
        s.SHARE_DEFAULT_LAYERS
    )


def test_share_defaults_routes_are_registered() -> None:
    get = s.ROUTE_SCHEMAS[("GET", "/api/settings/share-defaults")]
    put = s.ROUTE_SCHEMAS[("PUT", "/api/settings/share-defaults")]
    assert (get.request, get.query, get.response) == (None, None, s.ShareDefaultsOut)
    assert (put.request, put.query, put.response) == (s.ShareDefaults, None, s.ShareDefaultsOut)
