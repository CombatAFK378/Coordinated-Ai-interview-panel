from unittest.mock import Mock

import pytest

import ask_pipeline
from ask_pipeline import AskPipeline
from shared.interview_lookup import Hints
from shared.models import AskAnswer
from test_interview_lookup import NOW, ROWS


@pytest.fixture
def phone(settings, record, monkeypatch):
    """A phone-call pipeline with three saved interviews and a fake parser that
    understands the phrases used below."""
    hints = {
        "Vikram's 4:30 interview": Hints(name='Vikram', minutes=990),
        "Vikram's interview": Hints(name='Vikram'),
        "Ravi from last week": Hints(name='Ravi'),
        "now let's talk about Priya's interview": Hints(name='Priya'),
    }
    loaded = []

    def load_record(iid):
        loaded.append(iid)
        return record

    monkeypatch.setattr(ask_pipeline.store, 'list_summaries', lambda: list(ROWS))
    monkeypatch.setattr(ask_pipeline.store, 'load_record', load_record)
    monkeypatch.setattr(ask_pipeline.interview_lookup, 'now', lambda: NOW)
    monkeypatch.setattr(ask_pipeline.interview_lookup, 'parse_request',
                        lambda text, now: hints.get(text, Hints()))
    monkeypatch.setattr(ask_pipeline.time, 'sleep', lambda s: None)
    session, hangup = Mock(), Mock()
    pipeline = AskPipeline(session, settings, None, phone=True, on_hangup=hangup)
    pipeline._worker_state.generation = 0
    pipeline.loaded, pipeline.hangup = loaded, hangup
    return pipeline


def said(pipeline):
    return pipeline.session.speak.call_args.args[1]


def test_named_interview_loads_record(phone):
    phone._answer("Vikram's 4:30 interview")
    assert phone.loaded == ['a'] and phone.record is not None
    assert said(phone).startswith('Got it: Vikram, Backend Engineer, today at 4:28 PM.')
    assert 'Liam and Maya are on the line' in said(phone)


def test_ambiguous_then_pick_by_ordinal(phone):
    phone._answer("Vikram's interview")
    assert phone.record is None and said(phone).endswith('Which one?')
    phone._answer('the second one')
    assert phone.loaded == ['c']


def test_not_found_offers_what_exists(phone):
    phone._answer('Ravi from last week')
    assert phone.record is None
    assert said(phone).startswith("I couldn't find that one. Here's what I have:")


def test_unclear_request_asks_for_name(phone):
    phone._answer('hello?')
    assert 'Which candidate?' in said(phone) and phone.loaded == []


def test_switch_interview_mid_call(phone, monkeypatch):
    phone._answer("Vikram's 4:30 interview")
    phone._answer("now let's talk about Priya's interview")
    assert phone.loaded == ['a', 'b']


def test_questions_go_to_the_panel_once_loaded(phone, monkeypatch):
    answer = Mock(return_value=AskAnswer(question='q', mode='addressed', target='coding',
                                         answered_by='coding', answer='Grounded'))
    monkeypatch.setattr(ask_pipeline.ask_panel, 'answer', answer)
    phone._answer("Vikram's 4:30 interview")
    phone._answer('Hey Liam, why that coding score?')
    assert answer.call_args.kwargs['target'] == 'coding'
    assert phone.session.speak.call_args.args == ('coding', 'Grounded')


@pytest.mark.parametrize('text,bye', [
    ('ok thanks, bye', True),
    ("that's all, thank you", True),
    ('why did you say goodbye to him so early?', False),
    ("yes, that's it", False),
])
def test_goodbye_hangs_up(phone, text, bye):
    phone._answer(text)
    assert phone.hangup.called is bye
