import json

import pytest

from shared import orchestrator, scoring
from shared.models import AgentScore, Claim, TranscriptTurn


def claim(cid, text, competency='tech_depth', status='SOLID', strength=0.7):
    return Claim(id=cid, interview_id='t', text=text, competency=competency,
                 source_turn=1, strength=strength, status=status)


def model_returns(monkeypatch, payload, seen=None):
    def reason(messages, max_tokens=600):
        if seen is not None:
            seen.append(messages)
        return json.dumps(payload)
    monkeypatch.setattr(scoring, 'reason', reason)


GAPS = [claim(f'g{i}', f'Could not answer: how the RAG retrieval worked ({i})',
              status='VAGUE') for i in range(3)]
RESUME = [claim(f'r{i}', f'Built a RAG system ({i})', status='UNVERIFIED', strength=0.4)
          for i in range(3)]


def test_overall_follows_the_interviewers_own_competency_scores(monkeypatch):
    # The real case: tech depth 0.0, system design 0.0, but "overall 0.5".
    model_returns(monkeypatch, {'competency_scores': {'tech_depth': 0.0, 'system_design': 0.0},
                                'overall': 0.5, 'evidence': [], 'rationale': 'Could not explain.'})
    score = scoring.score_agent('technical', [], GAPS + RESUME, 't')
    assert score.overall == 0.0
    assert score.conviction == 'STRONG'     # three gaps are evidence; résumé claims are not


def test_overall_within_tolerance_is_kept(monkeypatch):
    model_returns(monkeypatch, {'competency_scores': {'tech_depth': 0.7}, 'overall': 0.8,
                                'evidence': [], 'rationale': ''})
    assert scoring.score_agent('technical', [], GAPS, 't').overall == 0.8


def test_unbacked_resume_claims_do_not_make_a_score_strong(monkeypatch):
    model_returns(monkeypatch, {'competency_scores': {'tech_depth': 0.1}, 'overall': 0.1,
                                'evidence': [], 'rationale': ''})
    assert scoring.score_agent('technical', [], RESUME, 't').conviction == 'NEUTRAL'


def conclusion(monkeypatch, rec, claims, answers, seen=None):
    model_returns(monkeypatch, {'recommendation': rec, 'headline': 'h', 'unresolved': [],
                                'reasoning': 'r'}, seen)
    scores = [AgentScore(interview_id='t', agent_id='technical', overall=0.8)]
    return scoring.conclude('t', scores, [], {'tech_depth': 0.5}, claims, answers)


def test_integrity_flag_is_never_a_clean_pass(monkeypatch):
    flagged = claim('c', 'Coding round: outside/AI help detected — integrity flag, coding '
                         'not demonstrated.', competency='coding')
    assert conclusion(monkeypatch, 'PROCEED', [flagged, *GAPS], 6).recommendation == 'PROCEED_FLAGGED'
    assert conclusion(monkeypatch, 'DECLINE', [flagged, *GAPS], 6).recommendation == 'DECLINE'


def test_chair_sees_gaps_and_only_gets_the_thin_hint_when_little_was_asked(monkeypatch):
    seen = []
    conclusion(monkeypatch, 'DECLINE', GAPS + RESUME, 5, seen)
    prompt = seen[-1][1]['content']
    assert '3 questions the candidate could not answer' in prompt
    assert '3 résumé claims never backed up' in prompt
    assert 'The candidate answered 5 questions.' in prompt and 'too little to judge' not in prompt
    conclusion(monkeypatch, 'INSUFFICIENT_SIGNAL', GAPS, 2, seen)
    assert 'too little to judge' in seen[-1][1]['content']


def test_could_not_answer_survives_bid_parsing():
    raw = json.dumps({'interest': 0.4, 'reason': 'gap', 'contradicts': 'Built a RAG system',
                      'claims_noticed': [{'text': 'Could not answer: how retrieval worked',
                                          'competency': 'tech_depth', 'strength': 0.7,
                                          'status': 'VAGUE'}]})
    _, _, claims, contradicts = orchestrator._parse_bid(raw)
    assert claims == [{'text': 'Could not answer: how retrieval worked',
                       'competency': 'tech_depth', 'strength': 0.7, 'status': 'VAGUE'}]
    assert contradicts == 'Built a RAG system'


def test_bid_prompt_names_the_question_just_answered():
    from shared import prompts
    transcript = [TranscriptTurn(seq=0, speaker='technical', text='How did retrieval work?'),
                  TranscriptTurn(seq=1, speaker='candidate', text='I do not remember.'),
                  TranscriptTurn(seq=2, speaker='hiring_manager', text='What did you own?'),
                  TranscriptTurn(seq=3, speaker='candidate', text='I do not remember either.')]
    user = prompts.build_bid_prompt('technical', transcript)[1]['content']
    assert 'asked by the Hiring Manager: "What did you own?"' in user


def test_report_counts_candidate_answers(monkeypatch):
    seen = []
    model_returns(monkeypatch, {'competency_scores': {}, 'overall': 0.5, 'evidence': [],
                                'rationale': '', 'action': 'HOLD', 'statement': 's',
                                'new_overall': 0.5, 'recommendation': 'DECLINE',
                                'headline': 'h', 'unresolved': [], 'reasoning': 'r'}, seen)
    transcript = [TranscriptTurn(seq=i, speaker='candidate' if i % 2 else 'technical', text='x')
                  for i in range(8)]
    scoring.build_report('t', ['technical'], transcript, GAPS, {'tech_depth': 0.5})
    assert 'The candidate answered 4 questions.' in seen[-1][1]['content']
