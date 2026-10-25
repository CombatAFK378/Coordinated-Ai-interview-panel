import json
from unittest.mock import Mock

import pytest

import main
import whatsapp
from shared import feedback
from shared.models import TranscriptTurn

PHONE = '+919800000009'


def model_says(monkeypatch, strengths, improve, closing='Keep going — you have a strong base.'):
    raw = json.dumps({'strengths': strengths, 'improve': improve, 'closing': closing})
    monkeypatch.setattr(feedback.scoring, 'reason', lambda *a, **k: raw)


@pytest.mark.parametrize('raw,expected', [
    ('+91 98765 43210', '+919876543210'),
    ('98765-43210', '+919876543210'),        # no country code → default
    ('+1 (415) 555-0123', '+14155550123'),
    ('call me', None),
    ('12', None),
])
def test_normalize_phone(raw, expected):
    assert feedback.normalize_phone(raw, '+91') == expected


def test_feedback_message_shape(record, monkeypatch):
    model_says(monkeypatch,
               ['You explained idempotency keys clearly with a concrete example.'],
               ['Practise talking through edge cases before coding, e.g. empty input.'])
    text = feedback.write_feedback(record, 'Vikram', 'Backend Engineer')
    assert text.startswith('Hi Vikram, thank you for interviewing for the Backend Engineer role.')
    assert '*What went well*\n• You explained idempotency keys' in text
    assert '*What to work on*\n• Practise talking through edge cases' in text
    assert ('Our team will evaluate your performance further and reach out to you.\n\n'
            '— Julian, on behalf of the interview panel') in text
    assert "replies aren't monitored" in text


@pytest.mark.parametrize('leak', [
    'Your coding score was 0.4, which held you back.',
    'The panel will not recommend you for the next round.',
    'You scored 6/10 on system design.',
    'Unfortunately we are declining your application.',
])
def test_points_that_leak_the_outcome_are_dropped(record, monkeypatch, leak):
    model_says(monkeypatch, ['Clear communication throughout.'], [leak, 'Quantify your impact.'])
    text = feedback.write_feedback(record, 'Vikram', 'Backend Engineer')
    assert leak not in text and 'Quantify your impact.' in text


def test_ordinary_words_are_not_mistaken_for_leaks(record, monkeypatch):
    point = 'You passed all the edge cases; before proceeding to code, restate the input.'
    model_says(monkeypatch, [point], ['Slow down when explaining trade-offs.'])
    assert point in feedback.write_feedback(record, 'Vikram', '')


def test_nothing_safe_to_send_raises(record, monkeypatch):
    model_says(monkeypatch, ['You scored well.'], ['We will decline.'])
    with pytest.raises(ValueError):
        feedback.write_feedback(record)


def test_context_never_includes_scores_or_the_recommendation(record):
    record.transcript = [TranscriptTurn(seq=0, speaker='candidate', text='I built a cache.')]
    ctx = feedback._context(record)
    assert 'I built a cache.' in ctx
    assert 'DECLINE' not in ctx and 'overall' not in ctx


# ---- sending ----------------------------------------------------------------

@pytest.fixture
def wa(settings):
    return settings.model_copy(update={'twilio_account_sid': 'ACtest', 'twilio_auth_token': 'secret',
                                       'twilio_whatsapp_from': 'whatsapp:+14155238886'})


def twilio(monkeypatch, statuses, code=None):
    """Fake Twilio: accepts the send, then reports `statuses` in turn."""
    sent = []
    monkeypatch.setattr(whatsapp.requests, 'post', lambda url, auth, data, timeout: (
        sent.append(data), Mock(status_code=201, json=lambda: {'sid': 'SM1'}))[1])
    seq = iter(statuses)
    monkeypatch.setattr(whatsapp.requests, 'get', lambda url, auth, timeout: Mock(
        json=lambda: {'status': next(seq), 'error_code': code}))
    monkeypatch.setattr(whatsapp.time, 'sleep', lambda s: None)
    monkeypatch.setattr(whatsapp.feedback, 'write_feedback', lambda *a: 'Hi Vikram, …')
    return sent


def test_feedback_is_sent_and_followed_to_delivery(wa, record, monkeypatch):
    sent = twilio(monkeypatch, ['queued', 'sent', 'delivered'])
    info = whatsapp.send_candidate_feedback(wa, record, PHONE, 'Vikram', 'Backend Engineer')
    assert sent[0]['To'] == f'whatsapp:{PHONE}' and sent[0]['Body'] == 'Hi Vikram, …'
    assert info['status'] == 'delivered' and info['text'] == 'Hi Vikram, …'


def test_closed_24_hour_window_is_reported(wa, record, monkeypatch):
    twilio(monkeypatch, ['queued', 'failed'], code=63016)
    info = whatsapp.send_candidate_feedback(wa, record, PHONE)
    assert info['status'] == 'not_delivered' and '24-hour window' in info['detail']


def test_write_failure_never_raises(wa, record, monkeypatch):
    monkeypatch.setattr(whatsapp.feedback, 'write_feedback',
                        Mock(side_effect=ValueError('model down')))
    info = whatsapp.send_candidate_feedback(wa, record, PHONE)
    assert info['status'] == 'error' and "couldn't write" in info['detail']


# ---- candidates never reach the panel ----------------------------------------

def test_candidates_are_blocked_when_there_is_no_allowlist(settings):
    open_chat = settings.model_copy(update={'whatsapp_allowed_numbers': ''})
    assert open_chat.whatsapp_allows('whatsapp:+919800000001', {PHONE})
    assert not open_chat.whatsapp_allows(f'whatsapp:{PHONE}', {PHONE})
    listed = settings.model_copy(update={'whatsapp_allowed_numbers': PHONE})
    assert listed.whatsapp_allows(f'whatsapp:{PHONE}', {PHONE})   # an allow-listed recruiter wins


def test_bad_phone_is_rejected_before_starting(settings, monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setattr(main, 'get_settings', lambda: settings)
    with TestClient(main.app) as client:
        resp = client.post('/session/start', json={'candidate_phone': 'call me'})
    assert resp.status_code == 400 and 'WhatsApp number' in resp.json()['detail']
