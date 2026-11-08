"""Model-visible filename contract and real correction over public HTTP/model transport."""
import json
from pathlib import Path
import re

import httpx
from fastapi.testclient import TestClient

from agent_harness_demo.model import ModelSettings
from agent_harness_demo.web import create_app
from test_completion import CompletionPeer
from test_workbench import create_task, finish, tool_reply


def test_model_sees_supported_filename_extensions_and_can_repair_an_invalid_name(tmp_path: Path) -> None:
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'facts.md').write_bytes(b'Owner: Mei\n')
    content = 'Owner: Mei [facts.md E1]'
    peer = CompletionPeer([
        tool_reply('plan_update', {'steps': ['Read and save']}, 'plan'),
        tool_reply('source_read', {'path': 'facts.md'}, 'read'),
        tool_reply('artifact_write', {'name': 'Release Handoff Summary', 'content': content, 'citations': ['E1']}, 'bad'),
        tool_reply('artifact_write', {'name': 'handoff.MD', 'content': content, 'citations': ['E1']}, 'good'),
        tool_reply('task_finish', {'summary': 'Saved.'}, 'finish'),
    ])
    with TestClient(create_app(tmp_path / 'store', model_settings=ModelSettings.local('contract-model', stream=False, thinking=True),
                               model_transport=httpx.MockTransport(peer))) as client:
        run, events = finish(client, create_task(client, source, sources=['facts.md']))
        assert run['status'] == 'completed', run['error']
        assert client.get(run['artifacts'][0]['download_url']).text.startswith(content)
    schema = next(t['function']['parameters']['properties']['name'] for t in peer.requests[2]['tools']
                  if t['function']['name'] == 'artifact_write')
    pattern = schema['pattern']
    assert '.md' in schema['description'] and '.txt' in schema['description']
    assert re.search(pattern, 'handoff.md') and re.search(pattern, 'notes.TXT') and re.search(pattern, 'handoff.MD')
    assert not re.search(pattern, 'Release Handoff Summary') and not re.search(pattern, '.hidden.md')
    error = next(e['data']['error'] for e in events if e['kind'] == 'correction.available')
    assert error['code'] == 'invalid_arguments' and error['correctable_fields'] == ['name']
    assert '.md' in error['message'] and '.txt' in error['message']
    assert any(e['kind'] == 'correction.observed' for e in events)
