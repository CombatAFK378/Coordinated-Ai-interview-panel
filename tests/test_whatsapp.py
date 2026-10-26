import base64
import hashlib
import hmac
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

import main
import whatsapp
from shared.interview_lookup import Hints
from shared.models import AskAnswer
from test_interview_lookup import NOW, ROWS

NUMBER = 'whatsapp:+919800000001'


def sign(token, url, params):
    data = url + ''.join(k + params[k] for k in sorted(params))
    return base64.b64encode(hmac.new(token.encode(), data.encode(), hashlib.sha1).digest()).decode()


@pytest.fixture
def wa_settings(settings, tmp_path):
    return settings.model_copy(update={
        'data_dir': str(tmp_path), 'twilio_account_sid': 'ACtest', 'twilio_auth_token': 'secret',
        'twilio_whatsapp_from': 'whatsapp:+14155238886',
        'whatsapp_allowed_numbers': '+919800000001, +919800000002'})


@pytest.fixture
def chat(wa_settings, record, monkeypatch):
    """Chats with three saved interviews, a fake parser and a fake panel."""
    hints = {"Vikram's 4:30 interview": Hints(name='Vikram', minutes=990),
             "now let's talk about Priya's interview": Hints(name='Priya')}
    monkeypatch.setattr(whatsapp.store, 'list_summaries', lambda: list(ROWS))
    monkeypatch.setattr(whatsapp.store, 'load_record', lambda iid: record)
    monkeypatch.setattr(whatsapp.store, 'save_record', Mock())
    monkeypatch.setattr(whatsapp.interview_lookup, 'now', lambda: NOW)
    monkeypatch.setattr(whatsapp.interview_lookup, 'parse_request',
                        lambda text, now: hints.get(text, Hints()))
    answer = Mock(side_effect=lambda rec, q, mode='open', target=None, channel='voice': AskAnswer(
        question=q, mode=mode, target=target, answered_by=target or 'orchestrator',
        answer=f'answer from {target or "orchestrator"}'))
    monkeypatch.setattr(whatsapp.ask_panel, 'answer', answer)
    sent = []
    chats = whatsapp.WhatsAppChats(wa_settings, send_fn=lambda s, to, body: sent.append(body))
    chats.sent, chats.answer = sent, answer
    return chats


def test_greeting_lists_interviews(chat):
    chat.handle(NUMBER, 'Hi')
    assert chat.sent[-1].startswith("*Julian · Host*\nHi, I'm Julian")
    assert '1. Vikram, Backend Engineer, today at 4:28 PM' in chat.sent[-1]
    assert '3. Vikram, Backend Engineer, yesterday at 10:15 AM' in chat.sent[-1]


def test_pick_by_number_then_named_question_and_followup(chat):
    chat.handle(NUMBER, 'hi')
    chat.handle(NUMBER, '2')
    assert chat.sent[-1].startswith('*Julian · Host*\nGot it: Priya, Backend Engineer, today at 11:00 AM.')
    chat.handle(NUMBER, 'Liam, why that coding score?')
    assert chat.sent[-1] == '*Liam · Coding Interviewer*\nanswer from coding'
    assert chat.answer.call_args.kwargs['channel'] == 'chat'
    chat.handle(NUMBER, 'and what was missing?')        # sticks with Liam, across saved state
    assert chat.sent[-1].startswith('*Liam · Coding Interviewer*')
    chat.handle(NUMBER, 'Julian, overall?')
    assert chat.sent[-1].startswith('*Julian · Host*\nanswer from orchestrator')


def test_name_lookup_then_switch(chat):
    chat.handle(NUMBER, "Vikram's 4:30 interview")
    assert 'Got it: Vikram, Backend Engineer, today at 4:28 PM' in chat.sent[-1]
    chat.handle(NUMBER, "now let's talk about Priya's interview")
    assert 'Got it: Priya' in chat.sent[-1]


@pytest.mark.parametrize('word', ['hi', 'Hello!', 'reset', 'start over'])
def test_greeting_or_reset_starts_fresh(chat, word):
    chat.handle(NUMBER, 'hi')
    chat.handle(NUMBER, '2')
    chat.handle(NUMBER, word)
    assert chat.sent[-1].startswith("*Julian · Host*\nHi, I'm Julian")
    chat.handle(NUMBER, 'Liam, why that coding score?')   # no interview loaded any more
    assert 'Which interview would you like to discuss?' in chat.sent[-1]
    chat.answer.assert_not_called()


def test_menu_keeps_the_interview_until_another_is_picked(chat):
    chat.handle(NUMBER, 'hi')
    chat.handle(NUMBER, '2')
    chat.handle(NUMBER, 'menu')
    assert 'Which interview next?' in chat.sent[-1]
    chat.handle(NUMBER, 'Liam, why that coding score?')
    assert chat.sent[-1].startswith('*Liam · Coding Interviewer*')


def test_unclear_first_message_offers_the_list(chat):
    chat.handle(NUMBER, 'can we talk')
    assert 'Which interview would you like to discuss?' in chat.sent[-1] and '1. ' in chat.sent[-1]


def test_override_by_chat_is_deterministic(chat, record):
    chat.handle(NUMBER, '1')                     # no list yet: falls back to the lookup
    chat.handle(NUMBER, 'hi')
    chat.handle(NUMBER, '1')
    chat.handle(NUMBER, 'decline the candidate')   # fixture is already DECLINE
    assert 'already declined' in chat.sent[-1]
    chat.handle(NUMBER, 'proceed with the candidate')
    assert "Done. I've overridden the panel's recommendation from decline to proceed" in chat.sent[-1]
    assert record.report.conclusion.recommendation == 'DECLINE'   # locked record untouched


def test_long_replies_are_split():
    parts = list(whatsapp._chunks('word ' * 700))
    assert len(parts) == 3 and all(len(p) <= 1600 for p in parts)


def test_signature():
    params = {'From': NUMBER, 'Body': 'hi', 'To': 'whatsapp:+14155238886'}
    url = 'https://example.ngrok-free.dev/whatsapp/webhook'
    good = sign('secret', url, params)
    assert whatsapp.valid_signature('secret', url, params, good)
    assert not whatsapp.valid_signature('secret', url, {**params, 'Body': 'bye'}, good)
    assert not whatsapp.valid_signature('other', url, params, good)
    assert not whatsapp.valid_signature('secret', url, params, None)


@pytest.fixture
def client(wa_settings, monkeypatch):
    main._teardown()
    chats = Mock()
    monkeypatch.setattr(main, 'get_settings', lambda: wa_settings)
    monkeypatch.setattr(main, 'WhatsAppChats', lambda s: chats)
    with TestClient(main.app) as c:
        c.chats = chats
        yield c


def post_webhook(client, params, token='secret'):
    url = 'http://testserver/whatsapp/webhook'
    return client.post('/whatsapp/webhook', data=params,
                       headers={'X-Twilio-Signature': sign(token, url, params)})


def test_webhook_queues_allowed_messages(client):
    resp = post_webhook(client, {'From': NUMBER, 'Body': 'hi'})
    assert resp.status_code == 200 and '<Response>' in resp.text
    client.chats.submit.assert_called_once_with(NUMBER, 'hi')


def test_webhook_rejects_bad_signature_and_ignores_strangers(client):
    assert post_webhook(client, {'From': NUMBER, 'Body': 'hi'}, token='wrong').status_code == 403
    assert post_webhook(client, {'From': 'whatsapp:+15550000000', 'Body': 'hi'}).status_code == 200
    client.chats.submit.assert_not_called()


def test_tunnel_serves_only_the_webhook(client):
    tunnel = {'X-Forwarded-For': '203.0.113.9'}
    assert client.get('/panel').status_code == 200
    assert client.get('/panel', headers=tunnel).status_code == 404
    assert client.get('/interviews', headers=tunnel).status_code == 404
    with pytest.raises(Exception):
        with client.websocket_connect('/session/events', headers=tunnel):
            pass
