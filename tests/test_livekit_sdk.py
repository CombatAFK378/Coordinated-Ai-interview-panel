"""Exercise the installed SDK with synthetic audio, without cloud/provider calls."""
import asyncio
import time
from types import SimpleNamespace

import numpy as np
from livekit import rtc
from livekit.agents import AgentSession, llm
from livekit.agents.voice import io
from livekit.local_inference import EOT

from livekit_session import LiveKitSession, PanelVoiceAgent


class AudioSink(io.AudioOutput):
    def __init__(self):
        super().__init__(label='test', capabilities=io.AudioOutputCapabilities(pause=False))
        self.frames = 0
        self.duration = 0

    async def capture_frame(self, frame):
        await super().capture_frame(frame)
        if not self.frames:
            self.on_playback_started(created_at=time.time())
        self.frames += 1
        self.duration += frame.duration

    def flush(self):
        super().flush()
        self.on_playback_finished(playback_position=self.duration, interrupted=False)

    def clear_buffer(self):
        self.on_playback_finished(playback_position=self.duration, interrupted=True)


class SynthStream:
    def __init__(self, owner):
        self.owner = owner

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    def push_text(self, text):
        self.owner.texts.append(text)

    def end_input(self):
        pass

    async def __aiter__(self):
        for _ in range(5):
            yield SimpleNamespace(frame=rtc.AudioFrame.create(24000, 1, 240))


class Synth:
    def __init__(self):
        self.texts = []

    def stream(self):
        return SynthStream(self)


def test_mini_model_executes_locally():
    with EOT() as model:
        probability = model.predict(np.zeros(19200, dtype=np.int16))
    assert 0 <= probability <= 1


def test_real_sdk_plays_selected_voice_audio_and_forwards_turn(settings):
    async def run():
        transport = LiveKitSession(settings, 'test', 'candidate')
        transport._voices = {'coding': Synth(), 'hiring_manager': Synth()}
        transport._voice = AgentSession(vad=None, turn_handling={'turn_detection': 'manual'})
        sink = AudioSink()
        transport._voice.output.audio = sink
        agent = PanelVoiceAgent(transport)
        received = []
        transport.on_text = received.append
        try:
            await transport._voice.start(agent=agent)
            for speaker, text in [('coding', 'Liam answer'), ('hiring_manager', 'Maya answer')]:
                result = await asyncio.wait_for(transport._speak(speaker, text), timeout=10)
                assert not result.interrupted
                assert result.text == text
                assert transport._voices[speaker].texts == [text]
            assert sink.frames == 10
            message = llm.ChatMessage(role='user', content=['Hey Liam, why?'])
            await agent.on_user_turn_completed(llm.ChatContext(), message)
            assert received == ['Hey Liam, why?']
        finally:
            await transport._voice.aclose()
    asyncio.run(run())


def test_failed_rtc_connection_closes_event_loop(settings, monkeypatch):
    async def fail_connect(*args, **kwargs):
        raise RuntimeError('controlled room connection failure')
    monkeypatch.setattr(rtc.Room, 'connect', fail_connect)
    transport = LiveKitSession(settings, 'test', 'candidate')
    import pytest
    with pytest.raises(RuntimeError, match='controlled room connection failure'):
        transport.start()
    transport.stop()  # cleanup is safe even after failed startup
    assert not transport._thread.is_alive()
    assert transport._loop.is_closed()
    assert transport._http.closed


def test_stt_provider_is_chosen_from_settings(settings, monkeypatch):
    import livekit_session
    monkeypatch.setattr(livekit_session.deepgram, 'STT', lambda **kw: ('deepgram', kw))
    monkeypatch.setattr(livekit_session.sarvam, 'STT', lambda **kw: ('sarvam', kw))
    session = LiveKitSession(settings, 'room', 'candidate')
    assert session._make_stt()[0] == 'deepgram'
    sarvam_settings = settings.model_copy(update={'stt_provider': 'sarvam', 'sarvam_api_key': 'sk',
                                                  'livekit_stt_language': 'en-IN'})
    name, kw = LiveKitSession(sarvam_settings, 'room', 'candidate')._make_stt()
    assert name == 'sarvam' and kw['model'] == 'saaras:v3' and kw['language'] == 'en-IN'
