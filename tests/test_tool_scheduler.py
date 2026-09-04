from charon.conversation.tool_scheduler import (
    execution_batches,
    tool_concurrency_mode,
)
from charon.providers import ToolCall


def _call(name, **arguments):
    return ToolCall(id=f'id-{name}-{len(arguments)}', name=name, arguments=arguments)


def test_argument_sensitive_concurrency_policy():
    assert tool_concurrency_mode('Read', {'path': 'x'}) == 'shared'
    assert tool_concurrency_mode('Http', {'method': 'GET'}) == 'shared'
    assert tool_concurrency_mode('Http', {'method': 'POST'}) == 'exclusive'
    assert tool_concurrency_mode('Git', {'command': 'git status'}) == 'shared'
    assert tool_concurrency_mode('Git', {'command': 'git commit -m x'}) == 'exclusive'
    assert tool_concurrency_mode('UnknownPlugin', {}) == 'exclusive'


def test_batches_keep_exclusive_barriers_and_report_limit_skips():
    calls = [
        _call('Read', path='a'),
        _call('Read', path='b'),
        _call('Edit', path='a'),
        _call('Web', query='q'),
        _call('Write', path='c'),
    ]
    batches, skipped = execution_batches(calls, limit=4)

    assert [[call.name for call in batch] for batch in batches] == [
        ['Read', 'Read'],
        ['Edit'],
        ['Web'],
    ]
    assert [call.name for call in skipped] == ['Write']
