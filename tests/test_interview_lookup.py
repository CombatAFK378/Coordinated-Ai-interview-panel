from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from shared import interview_lookup as lookup
from shared.interview_lookup import Hints

IST = ZoneInfo('Asia/Kolkata')
NOW = datetime(2026, 10, 24, 18, 0, tzinfo=IST)    # a Saturday


def row(iid, name, day, hh, mm, role='Backend Engineer', key='started_at'):
    return {'interview_id': iid, 'candidate_name': name, 'role': role,
            key: datetime(2026, 10, day, hh, mm, tzinfo=IST).timestamp()}


ROWS = [
    row('a', 'Vikram', 24, 16, 28),
    row('b', 'Priya', 24, 11, 0),
    row('c', 'Vikram', 23, 10, 15),
]


@pytest.mark.parametrize('heard,saved,ok', [
    ('Vikram Rao', 'Vikram', True),     # full name heard, first name saved
    ('vikrum', 'Vikram', True),                # STT misspelling
    ('Priya', 'Vikram', False),
    ('Liam', 'Liam', False),                     # panel names are never candidates
])
def test_name_score(heard, saved, ok):
    assert (lookup.name_score(heard, saved) >= lookup.NAME_THRESHOLD) is ok


def test_name_and_time_pick_one():
    found = lookup.match(Hints(name='Vikram Rao', minutes=16 * 60 + 30), ROWS, NOW)
    assert [r['interview_id'] for r in found] == ['a']


def test_name_alone_is_ambiguous_newest_first():
    found = lookup.match(Hints(name='Vikram'), ROWS, NOW)
    assert [r['interview_id'] for r in found] == ['a', 'c']


def test_day_and_latest():
    assert [r['interview_id'] for r in lookup.match(Hints(name='Vikram', day='yesterday'), ROWS, NOW)] == ['c']
    assert [r['interview_id'] for r in lookup.match(Hints(name='Vikram', day='friday'), ROWS, NOW)] == ['c']
    assert [r['interview_id'] for r in lookup.match(Hints(latest=True), ROWS, NOW)] == ['a']


def test_time_outside_window_matches_nothing():
    assert lookup.match(Hints(name='Priya', minutes=15 * 60), ROWS, NOW) == []


def test_within_limits_to_offered():
    found = lookup.match(Hints(minutes=10 * 60 + 15), ROWS, NOW, within=['a', 'c'])
    assert [r['interview_id'] for r in found] == ['c']


def test_older_records_fall_back_to_scored_time():
    old = row('d', 'Ravi', 24, 9, 40, key='created_at')
    assert lookup.match(Hints(name='Ravi', minutes=9 * 60 + 30), [old], NOW) == [old]


def test_describe():
    assert lookup.describe(ROWS[0], NOW) == 'Vikram, Backend Engineer, today at 4:28 PM'
    assert lookup.describe(ROWS[2], NOW) == 'Vikram, Backend Engineer, yesterday at 10:15 AM'


def test_parse_request(monkeypatch):
    monkeypatch.setattr(lookup.llm_router, 'chat',
                        lambda *a, **k: '{"name": "Vikram Rao", "time": "16:30", "day": "", "latest": false}')
    assert lookup.parse_request("Vikram's interview at four thirty", NOW) == Hints(
        name='Vikram Rao', minutes=990)


def test_parse_failure_falls_back_to_raw_words(monkeypatch):
    def fail(*a, **k):
        raise lookup.llm_router.LLMError('down')
    monkeypatch.setattr(lookup.llm_router, 'chat', fail)
    assert lookup.parse_request('Vikram please', NOW) == Hints(name='Vikram please')
