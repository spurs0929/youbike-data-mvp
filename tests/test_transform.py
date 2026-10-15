"""Tests for src/transform.py.

Run from the project root:

    pytest
"""

import pandas as pd
import pytest

from transform import TransformError, to_dataframe


def make_record(**overrides) -> dict:
    """One raw record. Values copied from a real response (2026-10-04)."""
    record = {
        "sno": "500101001",
        "sna": "YouBike2.0_捷運科技大樓站",
        "sarea": "大安區",
        "mday": "2026-10-04 19:31:02",
        "ar": "復興南路二段235號前",
        "sareaen": "Daan Dist.",
        "snaen": "YouBike2.0_MRT Technology Bldg. Sta.",
        "aren": "No.235， Sec. 2， Fuxing S. Rd.",
        "act": "1",
        "srcUpdateTime": "2026-10-04 19:31:52",
        "updateTime": "2026-10-04 19:31:52",
        "infoTime": "2026-10-04 19:31:02",
        "infoDate": "2026-10-04",
        "Quantity": 28,
        "available_rent_bikes": 17,
        "latitude": 25.02605,
        "longitude": 121.5436,
        "available_return_bikes": 11,
    }
    record.update(overrides)
    return record


def inactive_record() -> dict:
    """A real inactive station (act = 0, all counts 0)."""
    return make_record(
        sno="500101020",
        sna="YouBike2.0_溫州停車場",
        act="0",
        Quantity=5,
        available_rent_bikes=0,
        available_return_bikes=0,
    )


# --- normal conversion -------------------------------------------------------


def test_columns_are_renamed_and_extra_fields_dropped():
    df = to_dataframe([make_record()])
    assert list(df.columns) == [
        "station_id",
        "station_name",
        "district",
        "address",
        "latitude",
        "longitude",
        "total_spaces",
        "available_bikes",
        "available_spaces",
        "is_active",
        "info_time",
        "src_update_time",
        "update_time",
    ]


def test_values_are_converted():
    row = to_dataframe([make_record()]).iloc[0]
    assert row["station_id"] == "500101001"
    assert row["station_name"] == "捷運科技大樓站"
    assert row["total_spaces"] == 28
    assert row["available_bikes"] == 17
    assert row["available_spaces"] == 11
    assert bool(row["is_active"]) is True
    assert row["info_time"] == pd.Timestamp("2026-10-04 19:31:02", tz="Asia/Taipei")


def test_dtypes():
    df = to_dataframe([make_record()])
    assert df["station_id"].dtype == "string"
    assert df["total_spaces"].dtype == "Int64"
    assert df["latitude"].dtype == "float64"
    assert df["is_active"].dtype == "bool"
    assert str(df["info_time"].dt.tz) == "Asia/Taipei"


def test_inactive_station_is_kept():
    df = to_dataframe([make_record(), inactive_record()])
    assert len(df) == 2
    assert df["is_active"].tolist() == [True, False]


def test_numbers_sent_as_text_are_converted():
    df = to_dataframe([make_record(Quantity="28", latitude="25.02605")])
    assert df.loc[0, "total_spaces"] == 28
    assert df.loc[0, "latitude"] == pytest.approx(25.02605)


def test_station_id_sent_as_number_becomes_text():
    df = to_dataframe([make_record(sno=500101001)])
    assert df.loc[0, "station_id"] == "500101001"


def test_whitespace_is_stripped():
    df = to_dataframe([make_record(sarea=" 大安區 ")])
    assert df.loc[0, "district"] == "大安區"


# --- illegal act -------------------------------------------------------------


@pytest.mark.parametrize("act", ["2", "", "true", None])
def test_illegal_act_raises(act):
    with pytest.raises(TransformError, match="act"):
        to_dataframe([make_record(act=act)])


# --- missing values are kept as missing, not invented ------------------------


def test_missing_count_stays_missing_and_column_stays_integer():
    df = to_dataframe(
        [make_record(available_rent_bikes=None), make_record(sno="500101002")]
    )
    assert df["available_bikes"].dtype == "Int64"
    assert pd.isna(df.loc[0, "available_bikes"])
    assert df.loc[1, "available_bikes"] == 17


def test_missing_text_is_not_turned_into_the_word_none():
    df = to_dataframe([make_record(sna=None)])
    assert pd.isna(df.loc[0, "station_name"])


def test_missing_time_becomes_nat():
    df = to_dataframe([make_record(infoTime=None)])
    assert pd.isna(df.loc[0, "info_time"])


# --- duplicates --------------------------------------------------------------


def test_identical_duplicate_rows_are_dropped():
    df = to_dataframe([make_record(), make_record()])
    assert len(df) == 1


def test_same_station_with_different_values_raises():
    with pytest.raises(TransformError, match="500101001"):
        to_dataframe([make_record(), make_record(available_rent_bikes=3)])


# --- bad numbers -------------------------------------------------------------


@pytest.mark.parametrize(
    "field, value",
    [
        ("Quantity", "abc"),
        ("Quantity", 28.5),
        ("available_rent_bikes", "many"),
        ("latitude", "north"),
    ],
)
def test_bad_number_raises(field, value):
    with pytest.raises(TransformError, match="Column"):
        to_dataframe([make_record(**{field: value})])


# --- bad time format ---------------------------------------------------------


@pytest.mark.parametrize("field", ["infoTime", "srcUpdateTime", "updateTime"])
def test_bad_time_format_raises(field):
    with pytest.raises(TransformError, match="Column"):
        to_dataframe([make_record(**{field: "2026/10/04 19:31"})])


# --- required field missing -------------------------------------------------


@pytest.mark.parametrize("field", ["sno", "Quantity", "act", "infoTime"])
def test_missing_required_field_raises(field):
    record = make_record()
    del record[field]
    with pytest.raises(TransformError, match="Missing required columns"):
        to_dataframe([record])


def test_dropped_fields_are_not_required():
    record = make_record()
    for field in ("mday", "infoDate", "sareaen", "snaen", "aren"):
        del record[field]
    assert len(to_dataframe([record])) == 1
