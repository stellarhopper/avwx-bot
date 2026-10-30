import os
import shutil
from pathlib import Path

import pytest

from bot import fleet

CLUB = Path(__file__).parent / "fixtures" / "club"


def copy_club(tmp_path) -> Path:
    shutil.copytree(CLUB, tmp_path / "club")
    return tmp_path / "club"


def profile(club: Path) -> Path:
    return club / "aircraft" / "N9999X" / "aircraft.toml"


def test_loads_the_club_and_its_plane():
    club = fleet.load(CLUB)
    assert club.name == "Test Flying Club" and club.reserve_minutes == 45
    assert "mountain checkout" in club.rules
    plane = club.get("9999x")  # tails are normalized
    assert plane.type == "Cessna 182G" and plane.limits.usable_gal == 60
    assert set(plane.tables) == {"takeoff", "landing", "climb"}
    assert plane.manual("poh").sections["Emergency procedures"] == (2, 2)
    assert plane.max_demo_crosswind_kt == 15


def test_cruise_interpolates_by_power_then_altitude():
    plane = fleet.load(CLUB).get("N9999X")
    assert plane.cruise_at(2000) == (128.0, 11.0, "65% power")
    assert plane.cruise_at(4000)[:2] == (130.2, 11.0)
    tas, gph, note = plane.cruise_at(10000, 65)
    assert (tas, gph) == (135.0, 10.6) and "63% is the most the table gives at 10,000 ft" in note
    assert "highest table altitude" in plane.cruise_at(12000)[2]


def test_a_missing_value_rejects_the_profile_and_names_it(tmp_path):
    club = copy_club(tmp_path)
    p = profile(club)
    p.write_text(p.read_text().replace("empty_lb = 1780\n", ""))
    loaded = fleet.load(club)
    assert loaded.aircraft == {}
    assert "missing `empty_lb`" in loaded.problems[0]


def test_a_misshapen_table_rejects_the_profile(tmp_path):
    club = copy_club(tmp_path)
    p = profile(club)
    p.write_text(p.read_text().replace("[[340, 400, 470, 560],", "[[340, 400, 470],", 1))
    assert "expected 4 entries" in fleet.load(club).problems[0]


def test_tail_must_match_its_directory(tmp_path):
    club = copy_club(tmp_path)
    p = profile(club)
    p.write_text(p.read_text().replace('tail = "N9999X"', 'tail = "N1234"'))
    assert "doesn't match its directory" in fleet.load(club).problems[0]


def test_a_manual_must_exist_inside_manuals(tmp_path):
    club = copy_club(tmp_path)
    p = profile(club)
    p.write_text(p.read_text().replace('file = "poh.pdf"', 'file = "../aircraft.toml"'))
    assert "not found" in fleet.load(club).problems[0]


def test_the_cache_reloads_when_a_profile_changes(tmp_path):
    club = copy_club(tmp_path)
    cache = fleet.FleetCache(club)
    assert cache.get().get("N9999X").limits.empty_lb == 1780
    p = profile(club)
    p.write_text(p.read_text().replace("empty_lb = 1780", "empty_lb = 1790"))
    stat = p.stat()
    os.utime(p, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
    assert cache.get().get("N9999X").limits.empty_lb == 1790


def test_no_club_directory_is_an_empty_club(tmp_path):
    club = fleet.FleetCache(tmp_path / "nope").get()
    assert club.aircraft == {} and club.problems == []
