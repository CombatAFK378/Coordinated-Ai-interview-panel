from unittest.mock import Mock

import pytest

from shared import llm_router
from shared.config import Settings


@pytest.mark.parametrize('model_id,expected', [
    ('ollama:gpt-oss:120b', ('ollama', 'gpt-oss:120b')),
    ('ollama:minimax-m3', ('ollama', 'minimax-m3')),
    ('groq:openai/gpt-oss-120b', ('groq', 'openai/gpt-oss-120b')),
    ('nvidia/nemotron:free', ('openrouter', 'nvidia/nemotron:free')),
])
def test_provider_prefix(model_id, expected):
    assert llm_router._split(model_id) == expected


@pytest.mark.parametrize('provider,model,asked,sent', [
    ('ollama', 'gpt-oss:120b', 'low', 'low'),        # gpt-oss: effort just caps reasoning
    ('groq', 'openai/gpt-oss-120b', 'low', 'low'),
    ('ollama', 'minimax-m3', 'low', 'none'),         # thinking models: thinking off
    ('gemini', 'gemini-2.5-flash', 'low', 'none'),
    ('openrouter', 'google/gemma-4-31b-it:free', 'low', None),  # no knob: dropped
    ('ollama', 'minimax-m3', None, None),
])
def test_reasoning_effort_per_model(provider, model, asked, sent):
    assert llm_router._effort_for(provider, model, asked) == sent


def _response(status, content='ok'):
    resp = Mock(status_code=status)
    resp.json.return_value = {'choices': [{'message': {'content': content}}]}
    return resp


def test_fast_path_falls_back_from_ollama_to_groq_pool(monkeypatch):
    settings = Settings(_env_file=None, ollama_api_key='o-key', groq_api_keys='g1,g2')
    monkeypatch.setattr(llm_router, 'get_settings', lambda: settings)
    monkeypatch.setattr(llm_router.time, 'sleep', lambda s: None)
    calls = []

    def post(url, headers, json, timeout):
        calls.append((url, headers['Authorization'], json))
        return _response(429) if 'ollama.com' in url else _response(200, 'from groq')

    monkeypatch.setattr(llm_router.requests, 'post', post)
    assert llm_router.chat([{'role': 'user', 'content': 'hi'}], reasoning_effort='low') == 'from groq'
    ollama = [c for c in calls if 'ollama.com' in c[0]]
    assert len(ollama) == 3                                  # first try + 2 retries
    assert {c[1] for c in ollama} == {'Bearer o-key'}
    assert ollama[0][2]['model'] == 'gpt-oss:120b'
    assert 'groq.com' in calls[-1][0]
