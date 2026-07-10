"""Tests for memory extraction (LLM fact extraction from sessions)."""
from __future__ import annotations


from charon.memory.memory_extractor import (
    parse_extraction_response,
    extract_facts_sync,
    _format_session,
    extract_all_sessions,
)


class TestParseExtractionResponse:
    def test_valid_json_array(self):
        response = '''[
            {"content": "User graduated with Business Administration degree", "category": "biographical", "is_static": true},
            {"content": "User's commute is 45 minutes", "category": "biographical", "is_static": false}
        ]'''
        facts = parse_extraction_response(response)
        assert len(facts) == 2
        assert facts[0]["content"] == "User graduated with Business Administration degree"
        assert facts[0]["category"] == "biographical"
        assert facts[0]["is_static"] is True

    def test_markdown_code_block(self):
        response = '''Here are the facts:
```json
[{"content": "User prefers dark mode", "category": "preference", "is_static": true}]
```'''
        facts = parse_extraction_response(response)
        assert len(facts) == 1

    def test_invalid_json(self):
        facts = parse_extraction_response("This is not JSON at all")
        assert facts == []

    def test_empty_response(self):
        facts = parse_extraction_response("")
        assert facts == []

    def test_filters_short_content(self):
        response = '[{"content": "hi", "category": "general"}]'
        facts = parse_extraction_response(response)
        assert len(facts) == 0

    def test_normalizes_category(self):
        response = '[{"content": "User likes pizza for dinner", "category": "invalid_category"}]'
        facts = parse_extraction_response(response)
        assert facts[0]["category"] == "general"

    def test_preserves_event_date(self):
        response = '[{"content": "User visited MoMA", "category": "event", "is_static": false, "event_date": "2023-05-15"}]'
        facts = parse_extraction_response(response)
        assert facts[0]["event_date"] == "2023-05-15"

    def test_preserves_supersedes(self):
        response = '[{"content": "5K best is 25:50", "category": "event", "is_static": false, "supersedes": "5K best was 27:12"}]'
        facts = parse_extraction_response(response)
        assert facts[0]["_supersedes"] == "5K best was 27:12"

    def test_handles_non_list(self):
        response = '{"content": "single fact"}'
        facts = parse_extraction_response(response)
        assert facts == []

    def test_handles_mixed_valid_invalid(self):
        response = '''[
            {"content": "Valid fact about the user", "category": "biographical"},
            "not a dict",
            {"content": "", "category": "general"},
            {"content": "Another valid fact", "category": "preference"}
        ]'''
        facts = parse_extraction_response(response)
        assert len(facts) == 2


class TestFormatSession:
    def test_basic_format(self):
        session = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there!"},
        ]
        text = _format_session(session, "2023-05-15")
        assert "[Date: 2023-05-15]" in text
        assert "user: Hello" in text
        assert "assistant: Hi there!" in text

    def test_truncates_long_turns(self):
        session = [{"role": "user", "content": "x" * 5000}]
        text = _format_session(session)
        assert len(text) < 3000

    def test_handles_list_content(self):
        session = [{"role": "user", "content": [{"text": "Hello"}, {"text": "World"}]}]
        text = _format_session(session)
        assert "Hello World" in text


class TestExtractFactsSync:
    def test_returns_empty_without_llm(self):
        session = [{"role": "user", "content": "I graduated with a CS degree"}]
        facts = extract_facts_sync(session, "2023-05-15")
        assert facts == []

    def test_skips_short_sessions(self):
        session = [{"role": "user", "content": "Hi"}]
        facts = extract_facts_sync(session, llm_call=lambda m: "[]")
        assert facts == []

    def test_with_mock_llm(self):
        session = [
            {"role": "user", "content": "I just ran a 5K and my time was 27:12, a new personal best!"},
            {"role": "assistant", "content": "That's great! Congratulations on the personal best."},
        ]

        def mock_llm(messages):
            return '[{"content": "User set a 5K personal best of 27:12", "category": "event", "is_static": false}]'

        facts = extract_facts_sync(session, "2023-05-15", llm_call=mock_llm)
        assert len(facts) == 1
        assert "27:12" in facts[0]["content"]


class TestExtractAllSessions:
    def test_aggregates_facts(self):
        sessions = [
            [{"role": "user", "content": "I graduated with a Business Administration degree from State University."}],
            [{"role": "user", "content": "My daily commute to work takes about 45 minutes each way."}],
        ]
        dates = ["2023-05-01", "2023-05-15"]

        call_count = 0
        def mock_llm(messages):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return '[{"content": "User graduated with Business Administration from State University", "category": "biographical", "is_static": true}]'
            else:
                return '[{"content": "User commute is 45 minutes each way", "category": "biographical", "is_static": false}]'

        facts = extract_all_sessions(sessions, dates, llm_call=mock_llm)
        assert len(facts) == 2
        assert facts[0]["_session_date"] == "2023-05-01"
        assert facts[1]["_session_date"] == "2023-05-15"
