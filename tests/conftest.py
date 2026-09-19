import os
import tempfile

# main configures logging at import time. Never touch the developer's data/ logs.
os.environ['DATA_DIR'] = tempfile.mkdtemp(prefix='livekit-tests-')

import pytest
from shared.config import Settings
from shared.ask_panel import PanelRecord
from shared.models import AgentScore, InterviewReport, PanelConclusion


@pytest.fixture
def settings():
    return Settings(_env_file=None, livekit_url='ws://localhost:7880',
                    livekit_api_key='test-key', livekit_api_secret='x' * 40,
                    livekit_noise_cancellation='off', deepgram_api_key='test-deepgram',
                    groq_api_key='test-groq')


@pytest.fixture
def record():
    report = InterviewReport(
        interview_id='test-record',
        scores=[AgentScore(interview_id='test-record', agent_id=a) for a in ['coding','hiring_manager']],
        debate=[], conclusion=PanelConclusion(interview_id='test-record',
                                            recommendation='DECLINE', headline='Needs more evidence'),
        locked_hash='locked-original',
    )
    return PanelRecord('test-record', report, [], [])
