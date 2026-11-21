"""Tests for src/snapshot.py（4C-2 逐站驗證層）。

全部使用假資料與固定時間，不連網、不連資料庫。
"""

import copy
import dataclasses
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import snapshot
from snapshot import (
    PATH_FALLBACK,
    PATH_FAST,
    PATH_NONE,
    CleanMismatchError,
    validate_batch,
)
from transform import TransformError

# 擷取時間 06:20:00 UTC = 台灣 14:20:00。
FETCHED_AT = datetime(2026, 11, 21, 6, 20, 0, tzinfo=timezone.utc)
FRESH = "2026-11-21 14:18:00"  # 落後 2 分鐘


def record(sno="500101001", **overrides) -> dict:
    """一筆原始站點資料，欄位與真實回應相同。"""
    rec = {
        "sno": sno,
        "sna": "YouBike2.0_捷運科技大樓站",
        "sarea": "大安區",
        "mday": "2026-11-21 14:18:00",
        "ar": "復興南路二段235號前",
        "sareaen": "Daan Dist.",
        "snaen": "YouBike2.0_MRT Technology Bldg. Sta.",
        "aren": "No.235, Sec. 2, Fuxing S. Rd.",
        "act": "1",
        "srcUpdateTime": "2026-11-21 14:18:52",
        "updateTime": "2026-11-21 14:18:52",
        "infoTime": FRESH,
        "infoDate": "2026-11-21",
        "Quantity": 28,
        "available_rent_bikes": 17,
        "latitude": 25.02605,
        "longitude": 121.5436,
        "available_return_bikes": 11,
    }
    rec.update(overrides)
    return rec


def run(stations, fetched_at=FETCHED_AT):
    return validate_batch(stations, fetched_at)


def only(stations, fetched_at=FETCHED_AT):
    """驗證並回傳唯一一筆結果。"""
    batch = run(stations, fetched_at)
    assert len(batch.results) == 1
    return batch.results[0]


def by_id(batch):
    return {r.station_id: r for r in batch.results}


# ------------------------------------------------------------ 有效站


def test_valid_station_values():
    r = only([record()])

    assert r.station_id == "500101001"
    assert r.is_valid is True
    assert r.invalid_reason is None
    assert r.info_time == datetime(2026, 11, 21, 6, 18, 0, tzinfo=timezone.utc)
    assert r.info_time.utcoffset() == timedelta(0)
    assert r.is_active is True
    assert (r.total_spaces, r.available_bikes, r.available_spaces) == (28, 17, 11)
    assert r.info.station_name == "捷運科技大樓站"
    assert r.info.district == "大安區"
    assert r.info.address == "復興南路二段235號前"
    assert r.info.latitude == pytest.approx(25.02605)
    assert r.info.longitude == pytest.approx(121.5436)
    assert type(r.total_spaces) is int


def test_counts_as_numeric_strings_are_accepted():
    r = only(
        [
            record(
                Quantity=" 28 ", available_rent_bikes="17", available_return_bikes=11.0
            )
        ]
    )
    assert r.is_valid
    assert (r.total_spaces, r.available_bikes, r.available_spaces) == (28, 17, 11)


# ------------------------------------------------------------ 7 種無效原因


def test_duplicate_conflict():
    batch = run([record(available_rent_bikes=17), record(available_rent_bikes=16)])

    (r,) = batch.results
    assert r.invalid_reason == "duplicate_conflict"
    assert r.is_valid is False
    assert r.info is None
    assert r.info_time is None
    assert (r.total_spaces, r.available_bikes, r.available_spaces) == (None, None, None)
    assert r.is_active is True  # 兩筆 act 一致


@pytest.mark.parametrize(
    "field",
    ["act", "infoTime", "Quantity", "available_rent_bikes", "available_return_bikes"],
)
@pytest.mark.parametrize("value", [None, "", "   ", float("nan")])
def test_missing_field(field, value):
    r = only([record(**{field: value})])

    assert r.invalid_reason == "missing_field"
    assert r.info is None
    assert r.info_time is None
    assert r.total_spaces is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"act": "2"},
        {"act": 1},  # 只接受文字 "1"／"0"
        {"infoTime": "abc"},
        {"infoTime": "2026/11/21 14:18:00"},
        {"infoTime": 20261121},
        {"Quantity": "abc"},
        {"Quantity": 28.5},
        {"Quantity": True},
        {"available_rent_bikes": [1]},
        {"available_return_bikes": "1e3"},
    ],
)
def test_invalid_type(overrides):
    r = only([record(**overrides)])

    assert r.invalid_reason == "invalid_type"
    assert r.info is None
    assert r.total_spaces is None


def test_inactive_keeps_cleaned_values():
    r = only([record(act="0")])

    assert r.invalid_reason == "inactive"
    assert r.is_active is False
    assert r.total_spaces == 28
    assert r.info.station_name == "捷運科技大樓站"


def test_stale():
    r = only([record(infoTime="2026-11-21 14:05:00")])  # 落後 15 分鐘

    assert r.invalid_reason == "stale"
    assert r.is_active is True
    assert r.available_bikes == 17


def test_capacity_invalid():
    r = only([record(Quantity=-1, available_rent_bikes=0, available_return_bikes=0)])
    assert r.invalid_reason == "capacity_invalid"


@pytest.mark.parametrize(
    "overrides",
    [
        {"available_rent_bikes": 29},
        {"available_return_bikes": 29},
        {"available_rent_bikes": -1},
        {"available_return_bikes": -1},
    ],
)
def test_count_invalid(overrides):
    r = only([record(**overrides)])
    assert r.invalid_reason == "count_invalid"
    assert r.total_spaces == 28


# ------------------------------------------------------------ 判定順序


@pytest.mark.parametrize(
    ("stations", "expected"),
    [
        # duplicate_conflict 優先於 missing_field
        ([record(infoTime=None), record(infoTime=FRESH)], "duplicate_conflict"),
        # missing_field 優先於 invalid_type
        ([record(infoTime=None, Quantity="abc")], "missing_field"),
        # invalid_type 優先於 inactive
        ([record(act="0", Quantity="abc")], "invalid_type"),
        # inactive 優先於 stale
        ([record(act="0", infoTime="2026-11-21 13:00:00")], "inactive"),
        # stale 優先於 capacity_invalid
        ([record(infoTime="2026-11-21 13:00:00", Quantity=-1)], "stale"),
        # capacity_invalid 優先於 count_invalid
        ([record(Quantity=-1, available_rent_bikes=5)], "capacity_invalid"),
    ],
)
def test_reason_order(stations, expected):
    assert only(stations).invalid_reason == expected


def test_is_active_kept_independently_when_structure_invalid():
    assert only([record(act="0", Quantity="abc")]).is_active is False
    assert only([record(act="1", infoTime=None)]).is_active is True
    assert only([record(act="x")]).is_active is None


# ------------------------------------------------------------ 快速／備援路徑一致


def mixed_batch():
    """涵蓋各種結果的一批站點（本身可整批 clean）。"""
    return [
        record("A"),
        record("B", act="0"),
        record("C", infoTime="2026-11-21 14:00:00"),
        record("D", Quantity=-1),
        record("E", available_rent_bikes=99),
        record("F", Quantity=None),
        record("G", Quantity="abc"),
        record("H", available_rent_bikes=1),
        record("H", available_rent_bikes=2),
        record("I", latitude=999, sna="  "),
        record("J", Quantity=0, available_rent_bikes=0, available_return_bikes=0),
    ]


@pytest.mark.parametrize(
    "bad",
    [
        {"latitude": "abc"},
        {"longitude": "abc"},
        {"srcUpdateTime": "bad time"},
        {"updateTime": "bad time"},
    ],
)
def test_fallback_matches_fast_path(bad):
    clean_batch = run(mixed_batch())
    assert clean_batch.path == PATH_FAST

    mixed = run(mixed_batch() + [record("Z", **bad)])
    assert mixed.path == PATH_FALLBACK

    results = by_id(mixed)
    bad_station = results.pop("Z")
    assert results == by_id(clean_batch)
    assert [r.station_id for r in mixed.results][:-1] == [
        r.station_id for r in clean_batch.results
    ]

    # 壞的可選欄位只清成 NULL，該站仍有效，其他欄位保留。
    assert bad_station.is_valid
    assert bad_station.info.station_name == "捷運科技大樓站"
    if "latitude" in bad:
        assert bad_station.info.latitude is None
        assert bad_station.info.longitude == pytest.approx(121.5436)


def mixed_anomaly_batch():
    """正常站與各種異常混在同一批；本身可以整批 clean()（走快速路徑）。"""
    return [
        record("OK1"),
        record("OK2", Quantity="28", available_rent_bikes=17.0),
        record(sno=None),  # 跳過
        record(sno="   "),  # 跳過
        record("DUP", available_rent_bikes=1),  # 重複且衝突
        record("DUP", available_rent_bikes=2),
        record("SAME"),  # 重複且相同
        record("SAME"),
        record("T1", infoTime="abc"),  # 必要時間格式錯誤
        record("T2", infoTime=None),  # 必要時間缺失
        record("C1", latitude=999),  # 座標超出範圍
        record("C2", longitude=-181, latitude="Infinity"),
        record("C3", latitude="", longitude=None),
        record("STALE", infoTime="2026-11-21 14:00:00"),  # 過期
        record("NEG", infoTime="2026-11-21 14:25:00"),  # 負落後
        record("CAP", Quantity=-1),  # 容量異常
        record("CNT", available_rent_bikes=99),  # 車輛數異常
        record("OFF", act="0"),
        record("UNK", act="x"),
        record("TXT", sna="YouBike2.0_", sarea=" ", ar=None),
        record(" PAD "),
        record(500101009),
    ]


def all_stale_batch():
    """已知營運中站點全數過期，且有營運狀態未知的站（觸發整批告警）。"""
    old = "2026-11-21 14:00:00"
    return [
        record("A", infoTime=old),
        record("B", infoTime=old, latitude=999),
        record("C", act="0"),
        record("D", act="x"),
        record("E", infoTime=old, Quantity=-1),
    ]


@pytest.mark.parametrize("make_batch", [mixed_anomaly_batch, all_stale_batch])
def test_forced_fallback_matches_fast_path_on_identical_input(monkeypatch, make_batch):
    fast = run(make_batch())
    assert fast.path == PATH_FAST

    real_clean = snapshot.clean

    def single_row_only(df):
        # 整批呼叫一律失敗，強制改走逐站備援；單列呼叫照常清洗
        if len(df) > 1:
            raise TransformError("forced fallback")
        return real_clean(df)

    monkeypatch.setattr(snapshot, "clean", single_row_only)
    fallback = run(make_batch())

    assert fallback.path == PATH_FALLBACK
    # 逐站結果（is_valid、invalid_reason、清洗後欄位）與批次層統計都必須相同
    assert dataclasses.replace(fallback, path=fast.path) == fast


def test_mixed_anomaly_batch_expected_results():
    batch = run(mixed_anomaly_batch())
    results = by_id(batch)

    assert {sid: r.invalid_reason for sid, r in results.items()} == {
        "OK1": None,
        "OK2": None,
        "DUP": "duplicate_conflict",
        "SAME": None,
        "T1": "invalid_type",
        "T2": "missing_field",
        "C1": None,
        "C2": None,
        "C3": None,
        "STALE": "stale",
        "NEG": None,
        "CAP": "capacity_invalid",
        "CNT": "count_invalid",
        "OFF": "inactive",
        "UNK": "invalid_type",
        "TXT": None,
        "PAD": None,
        "500101009": None,
    }
    assert (results["C2"].info.latitude, results["C2"].info.longitude) == (None, None)
    assert results["C1"].info.latitude is None
    assert results["TXT"].info.station_name is None
    assert batch.skipped == 2
    assert batch.negative_lag == 1
    assert batch.unknown_active == 1
    assert batch.all_known_active_stale is False


def test_all_stale_batch_raises_alert():
    batch = run(all_stale_batch())
    assert batch.all_known_active_stale is True
    assert batch.unknown_active == 1


def test_path_none_when_nothing_to_clean():
    batch = run([record(sno=None), record(Quantity=None)])
    assert batch.path == PATH_NONE
    assert batch.skipped == 1
    assert len(batch.results) == 1


def test_clean_failure_after_structural_pass_is_a_bug(monkeypatch):
    def broken_clean(df):
        raise TransformError("boom")

    monkeypatch.setattr(snapshot, "clean", broken_clean)
    with pytest.raises(CleanMismatchError):
        run([record()])


# ------------------------------------------------------------ 邊界


def test_lag_exactly_threshold_is_valid():
    r = only([record(infoTime="2026-11-21 14:10:00")])  # 剛好 10 分鐘
    assert r.is_valid


def test_lag_just_over_threshold_is_stale():
    r = only([record(infoTime="2026-11-21 14:09:59")])  # 10 分 1 秒
    assert r.invalid_reason == "stale"


def test_negative_lag_is_counted_not_invalid():
    batch = run([record("A", infoTime="2026-11-21 14:21:00"), record("B")])

    assert batch.negative_lag == 1
    assert all(r.is_valid for r in batch.results)


@pytest.mark.parametrize("total", [0, 99])
def test_capacity_zero_and_99_are_valid(total):
    r = only(
        [
            record(
                Quantity=total,
                available_rent_bikes=total,
                available_return_bikes=0,
            )
        ]
    )
    assert r.is_valid
    assert r.total_spaces == total


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        (2**31 - 1, None),
        (2**31, "invalid_type"),
        (str(2**31), "invalid_type"),
        (-(2**31), "capacity_invalid"),
        (-(2**31) - 1, "invalid_type"),
        (float(2**40), "invalid_type"),
    ],
)
def test_integer_range(value, reason):
    r = only([record(Quantity=value, available_rent_bikes=0, available_return_bikes=0)])
    assert r.invalid_reason == reason


@pytest.mark.parametrize(
    ("overrides", "lat", "lon"),
    [
        ({"latitude": 999}, None, 121.5436),
        ({"longitude": -181}, 25.02605, None),
        ({"latitude": "abc"}, None, 121.5436),
        ({"latitude": "Infinity"}, None, 121.5436),
        ({"latitude": ""}, None, 121.5436),
        ({"latitude": None, "longitude": None}, None, None),
        ({"latitude": -90, "longitude": 180}, -90.0, 180.0),
        ({"latitude": "25.5"}, 25.5, 121.5436),
    ],
)
def test_coordinates(overrides, lat, lon):
    r = only([record(**overrides)])

    assert r.is_valid
    assert r.info.latitude == (lat if lat is None else pytest.approx(lat))
    assert r.info.longitude == (lon if lon is None else pytest.approx(lon))


def test_blank_text_becomes_none():
    r = only([record(sna="YouBike2.0_", sarea="  ", ar=None)])

    assert r.is_valid
    assert r.info.station_name is None
    assert r.info.district is None
    assert r.info.address is None


def test_identical_duplicates_keep_one():
    batch = run([record(), record()])

    (r,) = batch.results
    assert r.is_valid


def test_duplicates_differing_only_in_unwritten_fields_are_not_conflict():
    batch = run(
        [
            record(srcUpdateTime="2026-11-21 14:18:52", mday="x"),
            record(srcUpdateTime="2026-11-21 14:19:52", mday="y"),
        ]
    )
    (r,) = batch.results
    assert r.is_valid


def test_duplicate_conflict_with_different_act_has_unknown_is_active():
    batch = run([record(act="1"), record(act="0")])

    (r,) = batch.results
    assert r.invalid_reason == "duplicate_conflict"
    assert r.is_active is None


def test_duplicate_detected_after_stripping_sno():
    batch = run([record(sno=" 500101001 "), record(sno="500101001", Quantity=30)])

    (r,) = batch.results
    assert r.station_id == "500101001"
    assert r.invalid_reason == "duplicate_conflict"


# ------------------------------------------------------------ 前處理：跳過


@pytest.mark.parametrize("sno", [None, "", "   ", True, 1.5, ["1"]])
def test_unusable_sno_is_skipped(sno):
    batch = run([record(sno=sno), record("B")])

    assert batch.skipped == 1
    assert [r.station_id for r in batch.results] == ["B"]


def test_sno_is_stripped_and_int_accepted():
    batch = run([record(sno="  500101001 "), record(sno=500101002)])
    assert [r.station_id for r in batch.results] == ["500101001", "500101002"]
    assert all(r.is_valid for r in batch.results)


# ------------------------------------------------------------ 每站一筆、不改輸入


def test_one_result_per_station_in_first_seen_order():
    batch = run(mixed_batch())

    ids = [r.station_id for r in batch.results]
    assert len(ids) == len(set(ids))
    assert ids == ["A", "B", "C", "D", "E", "F", "G", "H", "I", "J"]


@pytest.mark.parametrize("extra", [[], [record("Z", latitude="abc")]])
def test_input_is_not_modified(extra):
    stations = mixed_batch() + [record(sno=" padded ")] + extra
    before = copy.deepcopy(stations)

    run(stations)

    assert stations == before


# ------------------------------------------------------------ 必要＋可選欄位同時錯誤


@pytest.mark.parametrize(
    ("required", "reason"),
    [
        ({"infoTime": None}, "missing_field"),
        ({"Quantity": "abc"}, "invalid_type"),
    ],
)
def test_required_reason_wins_over_optional_error(required, reason):
    r = only([record(latitude="abc", srcUpdateTime="bad", **required)])
    assert r.invalid_reason == reason


# ------------------------------------------------------------ 批次層統計


def test_all_known_active_stale_alert():
    old = "2026-11-21 14:00:00"
    batch = run(
        [
            record("A", infoTime=old),
            record("B", infoTime=old),
            record("C", act="0"),  # 暫停營運的站不影響判斷
            record("D", act="x"),  # 營運狀態未知
        ]
    )

    assert batch.all_known_active_stale is True
    assert batch.unknown_active == 1


def test_no_alert_when_any_known_active_station_is_fresh():
    batch = run([record("A", infoTime="2026-11-21 14:00:00"), record("B")])
    assert batch.all_known_active_stale is False


def test_no_alert_without_known_active_station():
    batch = run([record("A", act="0"), record("B", act="x")])

    assert batch.all_known_active_stale is False
    assert batch.unknown_active == 1


def test_alert_uses_lag_not_reason():
    # 營運中、新鮮但容量異常：原因不是 stale，仍代表來源沒有停滯。
    batch = run([record("A", infoTime="2026-11-21 14:00:00"), record("B", Quantity=-1)])
    assert batch.all_known_active_stale is False


# ------------------------------------------------------------ 時間


def test_naive_fetched_at_raises():
    naive = FETCHED_AT.replace(tzinfo=None)  # 刻意拿掉時區
    with pytest.raises(ValueError):
        run([record()], fetched_at=naive)


def test_non_utc_fetched_at_gives_same_result():
    taipei = timezone(timedelta(hours=8))
    same_moment = FETCHED_AT.astimezone(taipei)

    assert run(mixed_batch(), same_moment) == run(mixed_batch())


def test_module_does_not_read_current_time():
    source = (Path(__file__).parent.parent / "src" / "snapshot.py").read_text(
        encoding="utf-8"
    )
    assert "now(" not in source
    assert "time.time" not in source
