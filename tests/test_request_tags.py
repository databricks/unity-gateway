"""Tests for the request-tags header helper shared by agent launchers."""

from __future__ import annotations

from ucode.agents.request_tags import request_tags_header_value


class TestRequestTagsHeaderValue:
    def test_returns_stripped_value_from_explicit_env(self):
        env = {"AI_GATEWAY_REQUEST_TAGS": '  {"source":"my-app"}  '}
        assert request_tags_header_value(env) == '{"source":"my-app"}'

    def test_returns_none_when_absent(self):
        assert request_tags_header_value({}) is None

    def test_returns_none_when_blank(self):
        assert request_tags_header_value({"AI_GATEWAY_REQUEST_TAGS": "   "}) is None

    def test_reads_process_environment_by_default(self, monkeypatch):
        monkeypatch.setenv("AI_GATEWAY_REQUEST_TAGS", '{"source":"my-app"}')
        assert request_tags_header_value() == '{"source":"my-app"}'

    def test_process_environment_unset_returns_none(self, monkeypatch):
        monkeypatch.delenv("AI_GATEWAY_REQUEST_TAGS", raising=False)
        assert request_tags_header_value() is None

    def test_skips_and_warns_on_embedded_newline(self, capsys):
        pretty_json = '{\n  "source": "my-app"\n}'
        assert request_tags_header_value({"AI_GATEWAY_REQUEST_TAGS": pretty_json}) is None
        err = capsys.readouterr().err
        assert "AI_GATEWAY_REQUEST_TAGS" in err
        assert "line breaks" in err

    def test_skips_and_warns_on_carriage_return(self, capsys):
        value = '{"a":"b"}\r{"c":"d"}'
        assert request_tags_header_value({"AI_GATEWAY_REQUEST_TAGS": value}) is None
        assert "AI_GATEWAY_REQUEST_TAGS" in capsys.readouterr().err
