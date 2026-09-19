from unittest.mock import Mock

import jwt
import pytest
from fastapi.testclient import TestClient

import main
from shared.config import Settings
from shared.livekit_token import build_room_token


@pytest.fixture
def client(settings, monkeypatch):
    main._teardown()
    monkeypatch.setattr(main, 'get_settings', lambda: settings)
    main._report = main._panel_record = None
    with TestClient(main.app) as client:
        yield client
    main._report = main._panel_record = None


def test_token_scope(settings):
    token = build_room_token(settings, 'one-room', 'candidate')
    claims = jwt.decode(token, settings.livekit_api_secret, algorithms=['HS256'])
    assert claims['sub'] == 'candidate' and claims['video']['room'] == 'one-room'
    assert claims['video']['roomJoin'] is True
    assert claims['video']['agent'] is False
    assert claims['exp'] - claims['nbf'] == 3600
    assert 'roomAdmin' not in claims['video']


def test_missing_credentials_are_actionable(client, monkeypatch):
    monkeypatch.setattr(main, 'get_settings', lambda: Settings(_env_file=None))
    response = client.post('/session/start', json={})
    assert response.status_code == 503
    assert 'LIVEKIT_API_SECRET' in response.json()['detail']


def test_start_stop_and_scoped_browser_contract(client, monkeypatch):
    session = Mock()
    monkeypatch.setattr(main, 'LiveKitSession', Mock(return_value=session))
    response = client.post('/session/start', json={})
    assert response.status_code == 200
    body = response.json()
    assert body['server_url'].startswith('ws://')
    assert body['identity'] == 'candidate' and body['room'].startswith('iv-')
    assert 'app_id' not in body and 'api_secret' not in body
    session.start.assert_called_once()
    assert client.post('/session/stop').status_code == 200
    session.stop.assert_called_once()
    assert client.get('/health').json()['session_active'] is False


def test_failed_room_start_cleans_up(client, monkeypatch):
    session = Mock()
    session.start.side_effect = RuntimeError('connection failed')
    monkeypatch.setattr(main, 'LiveKitSession', Mock(return_value=session))
    response = client.post('/session/start', json={})
    assert response.status_code == 502
    session.stop.assert_called_once()
    assert main._session is None and main._pipeline is None


def test_panel_rejoin_uses_fresh_room_and_interrupt_works(client, record, monkeypatch):
    main._panel_record = record
    sessions = [Mock(), Mock()]
    for session in sessions:
        session.is_speaking.return_value = False
    monkeypatch.setattr(main, 'LiveKitSession', Mock(side_effect=sessions))
    first = client.post('/panel/join', json={}).json()
    second = client.post('/panel/join', json={}).json()
    assert first['room'] != second['room']
    assert second['identity'] == 'recruiter'
    sessions[0].stop.assert_called_once()
    assert client.post('/session/interrupt').status_code == 200
    assert not any('/cai/' in r.path for r in main.app.routes)


def test_typed_name_routes_and_locked_scores_survive(client, record, monkeypatch):
    main._panel_record = record
    monkeypatch.setattr('shared.llm_router.chat', lambda *args, **kwargs: 'Grounded reply')
    before = record.report.model_dump()
    response = client.post('/panel/ask', json={'question': 'Hey Liam, why that score?'})
    assert response.status_code == 200
    assert response.json()['answered_by'] == 'coding'
    assert record.report.model_dump() == before


def test_invalid_endpointing_config():
    with pytest.raises(ValueError):
        Settings(_env_file=None, livekit_endpointing_min_delay=5, livekit_endpointing_max_delay=1)
