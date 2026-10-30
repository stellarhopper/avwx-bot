from bot import route
from fakeapi import clients


async def resolve(*items):
    return await route.resolve(clients().awc, list(items))


async def test_fuzzy_route_as_a_member_would_say_it():
    res = await resolve("KBJC", "Rollins pass", "Granby", "Kremmling", "SBS", "KBJC")
    assert [r.status for r in res] == ["resolved"] * 6
    assert [r.point.ident for r in res] == ["KBJC", "Rollins Pass", "KGNB", "K20V", "KSBS", "KBJC"]
    assert route.describe([r.point for r in res]) == "KBJC → Rollins Pass (VPROL) → KGNB → K20V → KSBS → KBJC"


async def test_identifiers_fixes_and_navaids():
    res = await resolve("kbjc", "VPROL", "BJC VOR", "gnb")
    assert [(r.point.ident, r.point.kind) for r in res] == [
        ("KBJC", "airport"), ("VPROL", "fix"), ("BJC", "navaid"), ("KGNB", "airport"),
    ]
    assert res[1].point.lat == 39.9342


async def test_coordinates_both_ways():
    a, b = await resolve("39.93N 105.68W", "39.93,-105.68")
    assert a.point.position == b.point.position == (39.93, -105.68)
    assert a.point.ident == "39.93N/105.68W"


async def test_pass_by_alias_and_by_its_first_word():
    corona, vail = await resolve("Corona Pass", "Vail")
    assert corona.point.ident == "Rollins Pass"
    assert vail.point.ident == "Vail Pass"


async def test_a_far_off_namesake_loses_on_distance():
    # Sterling, CO is 100 nm from Denver; the Sterlings in MA and PA are not.
    _, sterling = await resolve("KDEN", "Sterling")
    assert sterling.status == "resolved" and sterling.point.ident == "KSTK"


async def test_two_nearby_namesakes_are_ambiguous():
    # Delta, CO has two public airports a few miles apart: ask, don't guess.
    _, delta, _ = await resolve("KGJT", "Delta", "KMTJ")
    assert delta.status == "ambiguous" and delta.point is None
    assert {c.point.ident for c in delta.candidates[:2]} == {"KAJZ", "KD17"}
    assert "ask which one" in delta.why


async def test_a_typo_is_offered_for_confirmation_not_taken():
    (kremling,) = await resolve("kremling")
    assert kremling.status == "ambiguous"
    assert [c.point.ident for c in kremling.candidates] == ["K20V"]
    assert "confirm" in kremling.why


async def test_nonsense_is_unresolved():
    (r,) = await resolve("xyzzy plugh")
    assert r.status == "unresolved" and r.point is None and r.candidates == ()


async def test_as_dict_carries_candidates_only_when_ambiguous():
    res = await resolve("KGJT", "Delta")
    assert "candidates" not in res[0].as_dict()
    assert res[1].as_dict()["candidates"][0]["kind"] == "airport"
