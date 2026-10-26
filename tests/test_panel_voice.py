import threading
from unittest.mock import Mock

import pytest

from ask_pipeline import AskPipeline, _detect_override, apply_override
from livekit_session import SpokenTurn
from pipeline import InterviewPipeline
from shared import ask_panel, prompts
from shared.models import AskAnswer
from shared.panel_routing import PanelRouter


@pytest.mark.parametrize('text,target', [
    ('Hey Liam, why that score?', 'coding'),
    ('hey MAYA, explain the evidence', 'hiring_manager'),
    ('Ethan, what do you think?', 'technical'),
    ('Sophia, explain the product assessment', 'product'),
    ('Nina, was the customer understood?', 'customer'),
    ('Julian, what was the decision?', 'orchestrator'),
    ('Ask HR about communication', 'hiring_manager'),
    ('Walk me through the strengths', None),
    ('William had a good example', None),
])
def test_names_and_word_boundaries(text, target):
    assert PanelRouter.detect(text) == target


def test_followups_and_reset():
    router = PanelRouter()
    assert router.resolve('Hey Liam, why that score?') == 'coding'
    assert router.resolve('Can you give a concrete example?') == 'coding'
    assert router.resolve('Maya, what about you?') == 'hiring_manager'
    assert router.resolve('What is the overall recommendation?') is None
    assert router.resolve('Hey Liam, your overall impression?') == 'coding'
    assert router.resolve('Julian, explain the outcome') is None


@pytest.mark.parametrize('text', ['Why did you reject the candidate?',
                                 'Explain why you would accept this candidate',
                                 'override', 'override accept and reject'])
def test_questions_and_ambiguous_commands_do_not_override(text):
    assert _detect_override(text) is None


@pytest.mark.parametrize('text,decision', [
    ('No worries i want you to ovveride the decesion with proceed', 'PROCEED'),
    ('overide it and decline', 'DECLINE'),
    ('overrule the panel and hire him', 'PROCEED'),
    ('the overdue report should proceed', None),       # not an override word
])
def test_override_tolerates_typos(text, decision):
    result = _detect_override(text)
    assert (result[0] if result else None) == decision


def test_bare_override_asks_which_way(record):
    reply, ov = apply_override(record, 'please ovveride it')
    assert ov is None and reply.startswith('Which way should I override it')
    assert apply_override(record, 'what was the overview?') is None


def test_latency_line_adds_up_the_turn(settings, record, caplog):
    session = Mock(last_turn_end_s=0.6, last_first_audio_s=0.3)
    with caplog.at_level('INFO'):
        AskPipeline(session, settings, record)._log_latency(0.7)
    assert ('turn end 0.60s + answer 0.70s + first audio 0.30s = 1.60s '
            "from your last word to the panel's voice") in caplog.text


def test_named_answer_uses_that_personas_voice(settings, record, monkeypatch):
    def answer(record, question, mode='open', target=None):
        return AskAnswer(question=question, mode=mode, target=target,
                         answered_by=target or 'orchestrator', answer='Grounded answer')
    monkeypatch.setattr(ask_panel, 'answer', answer)
    session = Mock()
    pipeline = AskPipeline(session, settings, record)
    pipeline._worker_state.generation = 0
    for question in ['Hey Liam, explain your score', 'Give an example',
                     'Hey Maya, what concerned you?', 'Julian, overall decision']:
        pipeline._answer(question)
    assert [c.args[0] for c in session.speak.call_args_list] == [
        'coding', 'coding', 'hiring_manager', 'orchestrator']
    assert len({a.voice_model for a in prompts.AGENTS.values()}) == 6


def test_absent_interviewer_does_not_invent_assessment(record, monkeypatch):
    generate = Mock(side_effect=AssertionError('must not call the model'))
    monkeypatch.setattr(ask_panel.llm_router, 'chat', generate)
    result = ask_panel.answer(record, 'Hey Nina, what did you assess?', 'addressed', 'customer')
    assert result.answered_by == 'customer'
    assert "wasn't on the panel" in result.answer
    generate.assert_not_called()


def test_override_can_be_reversed_without_mutating_report(settings, record, monkeypatch):
    monkeypatch.setattr('ask_pipeline.store.save_record', lambda rec: None)
    pipeline = AskPipeline(Mock(), settings, record)
    pipeline._worker_state.generation = 0
    before = record.report.model_dump()
    pipeline._answer('override and accept the candidate')
    pipeline._answer('override and reject the candidate')
    assert [o.decision for o in record.overrides] == ['PROCEED', 'DECLINE']
    assert record.report.model_dump() == before


def test_interrupted_stale_reply_cannot_speak(settings, record, monkeypatch):
    started, release, spoken = threading.Event(), threading.Event(), threading.Event()
    def answer(record, question, mode='open', target=None):
        if 'Liam' in question:
            started.set()
            assert release.wait(2)
        return AskAnswer(question=question, mode=mode, target=target,
                         answered_by=target or 'orchestrator', answer=question)
    monkeypatch.setattr(ask_panel, 'answer', answer)
    session = Mock()
    session.speak.side_effect = lambda *args: spoken.set()
    pipeline = AskPipeline(session, settings, record)
    pipeline.start()
    try:
        pipeline.submit_text('Hey Liam, explain your score')
        assert started.wait(2)
        pipeline.submit_text('Hey Maya, answer instead')
        release.set()
        assert spoken.wait(2)
        assert [c.args[0] for c in session.speak.call_args_list] == ['hiring_manager']
    finally:
        release.set()
        pipeline.stop()


def test_interview_keeps_all_completed_user_turns(settings):
    pipeline = InterviewPipeline(Mock(), settings, 'test')
    pipeline.submit_text('I built the service')
    pipeline.submit_text('And I owned the deployment')
    assert [t.text for t in pipeline.transcript] == ['I built the service', 'And I owned the deployment']


def test_interrupted_audio_uses_played_transcript(settings):
    session = Mock()
    session.speak.return_value = SpokenTurn('Please explain', True)
    pipeline = InterviewPipeline(session, settings, 'test')
    pipeline._speak_and_wait('coding', 'Please explain the algorithm and its complexity.')
    turn = pipeline.transcript[-1]
    assert turn.truncated and turn.truncation_char == len('Please explain')
    assert pipeline.audit[-1].type == 'interruption'


def test_redo_restores_evidence_and_floor(settings):
    import copy
    session = Mock()
    pipeline = InterviewPipeline(session, settings, 'test')
    pipeline._redo_snapshot = (1, copy.deepcopy(pipeline.ledger), copy.deepcopy(pipeline.floor), [], 0)
    pipeline._add_turn('candidate', 'A misheard answer')
    pipeline.ledger.add(text='Misheard evidence', competency='coding', source_turn=1,
                        strength=0.8, status='SOLID', noticed_by='coding')
    assert pipeline.request_redo()
    assert pipeline.transcript == [] and pipeline.ledger.claims() == []
    session.clear_user_turn.assert_called_once()
