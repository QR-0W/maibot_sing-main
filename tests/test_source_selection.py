"""Offline tests for conservative source selection."""

from __future__ import annotations

from dataclasses import replace

import pytest

from runtime.source_selection import (
    InvalidSourceSelection,
    SourceAmbiguous,
    SourceCandidate,
    SourceUnavailable,
    select_source,
)


def song(identifier: str, *, album: str = "Original", duration: float = 210, size: int | None = 8000,
         source: str = "163") -> SourceCandidate:
    return SourceCandidate(source, identifier, "Song", "Artist", album, duration, size)


def choose(candidates: list[SourceCandidate], **kwargs: object) -> SourceCandidate:
    options = dict(title="Song", artist="Artist", max_seconds=300, max_bytes=10000)
    options.update(kwargs)
    return select_source(candidates, **options)


def test_exact_stable_id_duplicates_only() -> None:
    original = song("a")
    assert choose([original, original]) == original
    with pytest.raises(SourceAmbiguous) as exc:
        choose([original, song("b", duration=210)])
    assert exc.value.code == "ambiguous"
    assert [c.identifier for c in exc.value.candidates] == ["a", "b"]


def test_pinkpop_live_inside_presumed_studio_duration_cluster_is_ambiguous() -> None:
    releases = [song("studio-1", album="Original", duration=211),
                song("pinkpop", album="Pinkpop Live", duration=212),
                song("studio-2", album="Compilation", duration=213),
                song("other", album="Remix", duration=245)]
    with pytest.raises(SourceAmbiguous) as exc:
        choose(releases)
    assert {(c.album, c.identifier, c.duration_s) for c in exc.value.candidates} == {
        (c.album, c.identifier, c.duration_s) for c in releases}
    assert choose(releases, source_id="pinkpop") == releases[1]
    assert choose(releases, album="Original") == releases[0]


def test_album_must_identify_one_candidate_and_source_id_can_disambiguate() -> None:
    releases = [song("a", album="Same"), song("b", album="Same")]
    with pytest.raises(SourceAmbiguous):
        choose(releases, album="same")
    assert choose(releases, album=" SAME ", source_id="b") == releases[1]
    with pytest.raises(InvalidSourceSelection) as exc:
        choose(releases, album="Other")
    assert exc.value.code == "invalid_selection"
    assert len(exc.value.candidates) == 2


def test_source_namespaces_are_distinct() -> None:
    releases = [song("123", source="163"), song("123", source="qq")]
    with pytest.raises(SourceAmbiguous):
        choose(releases, source_id="123")
    assert choose(releases, source_id="123", source="qq") == releases[1]


def test_exact_metadata_and_bounded_full_song_only() -> None:
    baseline = song("ok")
    bad = [replace(baseline, identifier="wrong-title", title="Song Live"),
           replace(baseline, identifier="wrong-artist", artist="Another Artist"),
           song("short", duration=29.9), song("long", duration=300.1),
           song("oversize", size=10001), song("unknown-size", size=None),
           song("zero-size", size=0), song("nan", duration=float("nan"))]
    assert choose(bad + [baseline]) == baseline
    with pytest.raises(SourceUnavailable) as exc:
        choose(bad)
    assert exc.value.code == "unavailable"
    assert not exc.value.candidates
    assert choose([replace(baseline, title="Ｓｏｎｇ", artist="ARTIST")])


def test_conflicting_metadata_and_invalid_selectors_fail_closed() -> None:
    with pytest.raises(InvalidSourceSelection):
        choose([song("a"), song("a", album="Other")])
    with pytest.raises(InvalidSourceSelection):
        choose([song("a")], source="163")
    with pytest.raises(InvalidSourceSelection):
        choose([song("a")], source_id="missing")
    with pytest.raises(InvalidSourceSelection):
        choose([song("a")], title="")
    with pytest.raises(InvalidSourceSelection):
        choose([song("a")], max_bytes=-1)
