"""Outgoing model request shape: prompt caching, old tool-result stubs, web tools, suggestion parsing."""
import copy

from app import config
from app.kaisen import llm


def _turn(i, name="read_file", path="app.py", size=2200):
    tid = f"toolu_{i}"
    return [
        {"role": "assistant", "content": [{"type": "tool_use", "id": tid, "name": name, "input": {"path": path}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tid, "content": "x" * size}]},
    ]


def _history(turns, **kw):
    msgs = [{"role": "user", "content": "Project name: t\n\n<brief>b</brief>"}]
    for i in range(turns):
        msgs += _turn(i, **kw)
    return msgs


def _marks(req):
    n = sum(1 for t in req["tools"] if "cache_control" in t) + sum(1 for b in req["system"] if "cache_control" in b)
    for m in req["messages"]:
        if isinstance(m["content"], list):
            n += sum(1 for b in m["content"] if isinstance(b, dict) and "cache_control" in b)
    return n


def test_cache_breakpoints_on_last_tool_system_and_latest_turn_within_limit():
    req = llm.build_request("claude-sonnet-5-5", _history(3), "medium")
    assert "cache_control" in req["tools"][-1] and "cache_control" not in req["tools"][0]
    assert req["system"][0]["cache_control"] == {"type": "ephemeral"}
    last = req["messages"][-1]["content"][-1]
    assert last["cache_control"] == {"type": "ephemeral"}
    assert sum("cache_control" in b for m in req["messages"] if isinstance(m["content"], list) for b in m["content"]) == 1
    assert _marks(req) == 3 <= 4


def test_string_seed_message_becomes_a_marked_text_block():
    req = llm.build_request("claude-sonnet-5-5", [{"role": "user", "content": "hello"}], "low")
    assert req["messages"][0]["content"] == [{"type": "text", "text": "hello", "cache_control": {"type": "ephemeral"}}]


def test_cache_mark_skips_thinking_blocks_and_does_not_touch_stored_messages():
    msgs = [{"role": "user", "content": "hi"},
            {"role": "assistant", "content": [{"type": "text", "text": "t"}, {"type": "thinking", "thinking": "x", "signature": "s"}]}]
    before = copy.deepcopy(msgs)
    req = llm.build_request("claude-sonnet-5-5", msgs, "low")
    blocks = req["messages"][-1]["content"]
    assert "cache_control" in blocks[0] and "cache_control" not in blocks[1]
    assert msgs == before


def test_old_tool_results_become_stubs_in_the_request_only():
    msgs = _history(config.KAISEN_ELIDE_KEEP_TURNS + config.KAISEN_ELIDE_STRIDE)
    before = copy.deepcopy(msgs)
    out = llm.prepare_messages(msgs)
    assert msgs == before  # stored history untouched
    first = out[2]["content"][0]["content"]
    assert first == "[old tool result elided: read_file app.py, 2.1 KB]"
    assert out[-1]["content"][0]["content"] == "x" * 2200  # newest turns stay in full
    assert out[2]["content"][0]["tool_use_id"] == "toolu_0"


def test_nothing_is_elided_while_the_history_is_short():
    msgs = _history(config.KAISEN_ELIDE_KEEP_TURNS)
    assert llm.prepare_messages(msgs) == msgs


def test_boundary_moves_in_strides_so_the_cached_prefix_stays_stable():
    keep, stride = config.KAISEN_ELIDE_KEEP_TURNS, config.KAISEN_ELIDE_STRIDE
    base = _history(keep + stride)
    a = llm.prepare_messages(base)
    b = llm.prepare_messages(_history(keep + stride + stride - 1))
    assert a == b[:len(a)]  # one more turn, same prefix: cache hit
    c = llm.prepare_messages(_history(keep + 2 * stride))
    assert c[:len(a)] != a[:len(a)]  # the boundary jumped once per stride


def test_short_results_ask_human_answers_and_errors_are_kept():
    keep, stride = config.KAISEN_ELIDE_KEEP_TURNS, config.KAISEN_ELIDE_STRIDE
    msgs = [{"role": "user", "content": "seed"}]
    msgs += _turn(0, name="write_file", size=20)                    # short: kept
    msgs += _turn(1, name="ask_human", size=900)                    # human's answer: kept
    err = _turn(2, size=900)
    err[1]["content"][0]["is_error"] = True                         # long error: stubbed but still flagged
    msgs += err
    for i in range(3, keep + stride + 3):
        msgs += _turn(i)
    out = llm.prepare_messages(msgs)
    assert out[2]["content"][0]["content"] == "x" * 20
    assert out[4]["content"][0]["content"] == "x" * 900
    assert out[6]["content"][0]["content"].startswith("[old tool result elided") and out[6]["content"][0]["is_error"] is True


def test_web_tools_only_for_supported_models_with_domains_and_limits():
    off = llm.build_request("claude-sonnet-5-5", _history(1), "low")
    assert [t["name"] for t in off["tools"]] == [t["name"] for t in llm.TOOLS]
    assert "Web access" not in off["system"][0]["text"] and "no network access" in off["system"][0]["text"]
    on = llm.build_request("claude-sonnet-5-5", _history(1), "low", web={"allowed_domains": ["docs.example.com"]})
    srv = {t["name"]: t for t in on["tools"] if t["name"] in ("web_search", "web_fetch")}
    assert srv["web_search"]["type"] == config.KAISEN_WEB_SEARCH_TOOL and srv["web_fetch"]["type"] == config.KAISEN_WEB_FETCH_TOOL
    for t in srv.values():
        assert t["max_uses"] == config.KAISEN_WEB_MAX_USES and t["allowed_domains"] == ["docs.example.com"]
    assert "UNTRUSTED" in on["system"][0]["text"] and "{{" not in on["system"][0]["text"]
    assert _marks(on) == 3
    nodomains = llm.build_request("claude-sonnet-5-5", _history(1), "low", web={"allowed_domains": []})
    assert all("allowed_domains" not in t for t in nodomains["tools"])


def test_unsupported_model_omits_web_tools(monkeypatch):
    monkeypatch.setattr(config, "KAISEN_WEB_MODELS", ("claude-opus-5-5",))
    req = llm.build_request("claude-haiku-5-5", _history(1), "low", web={"allowed_domains": []})
    assert [t["name"] for t in req["tools"]] == [t["name"] for t in llm.TOOLS]
    assert "UNTRUSTED" not in req["system"][0]["text"]


def test_prompt_has_plan_selfcheck_and_next_instructions():
    text = llm.build_system()
    assert "PLAN.md" in text and "Self-check" in text and "Next:" in text and "{{" not in text


def test_parse_suggestions_strips_next_lines_and_caps():
    summary = ("Built a todo app.\nRun: python app.py\n\nNext: add due dates\n- Next: dark mode\n"
               "**Next:** export to CSV\nNext: a fourth idea\nNext: add due dates")
    clean, sugg = llm.parse_suggestions(summary)
    assert clean == "Built a todo app.\nRun: python app.py"
    assert sugg == ["add due dates", "dark mode", "export to CSV"]
    assert len(sugg) == config.KAISEN_SUGGESTIONS_MAX


def test_parse_suggestions_clips_and_handles_none():
    clean, sugg = llm.parse_suggestions("Done.\nNext: " + "y" * 500)
    assert clean == "Done." and len(sugg[0]) == config.KAISEN_SUGGESTION_MAX_CHARS
    assert llm.parse_suggestions("Just a summary, mentioning the next step inline.") == ("Just a summary, mentioning the next step inline.", [])


def test_anthropic_client_sends_the_built_request_and_reads_server_tool_usage():
    from types import SimpleNamespace as NS

    seen = {}

    class Msg:
        stop_reason = "end_turn"
        content = []
        usage = NS(input_tokens=5, output_tokens=6, cache_creation_input_tokens=1, cache_read_input_tokens=2,
                   server_tool_use=NS(web_search_requests=3))

    class Stream:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get_final_message(self):
            return Msg()

    def stream(*, model, max_tokens, system, tools, messages, output_config=None, **kw):
        seen.update(tools=tools, messages=messages, output_config=output_config)
        return Stream()

    client = llm.AnthropicClient(client=NS(messages=NS(stream=stream)))
    resp = client.create(model="claude-sonnet-5-5", messages=_history(1), effort="low", web={"allowed_domains": []})
    assert resp.usage["web_search_requests"] == 3 and resp.usage["cache_read_input_tokens"] == 2
    assert {"web_search", "web_fetch"} <= {t["name"] for t in seen["tools"]} and seen["output_config"] == {"effort": "low"}
