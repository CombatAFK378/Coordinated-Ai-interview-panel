from unittest.mock import Mock

import pytest

import main


@pytest.fixture
def phone_app(settings, monkeypatch):
    """main with voice stubbed out, so a call can be taken without LiveKit."""
    main._teardown()
    monkeypatch.setattr(main, '_voice_settings', lambda: settings)
    monkeypatch.setattr(main, 'LiveKitSession', Mock())
    monkeypatch.setattr(main, 'AskPipeline', Mock())
    monkeypatch.setattr(main, '_start_voice', Mock())
    monkeypatch.setattr(main, '_begin_session_logs', Mock())
    yield main
    main._pipeline = None
    main._teardown()


def test_call_refused_while_an_interview_is_in_progress(phone_app):
    phone_app._pipeline = Mock(concluded=False, interview_id='live')
    assert phone_app._on_phone_call('call-1') is None
    phone_app._pipeline.stop.assert_not_called()


def test_call_taken_once_the_interview_is_scored(phone_app):
    finished = Mock(concluded=True, interview_id='scored')
    phone_app._pipeline = finished
    done = phone_app._on_phone_call('call-2')
    assert done is not None and not done.is_set()
    finished.stop.assert_called_once()          # the finished session was closed
    assert phone_app._on_phone_call('call-3') is None   # one call at a time
