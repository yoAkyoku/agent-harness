"""Public model transport test for a mixed plan and aggregate near the context limit."""
import json
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from agent_harness_demo.model import ModelSettings
from agent_harness_demo.web import create_app
from test_completion import CompletionPeer
from test_json_decisions import json_reply
from test_session_versions import qwen_metadata
from test_workbench import create_task, finish


@pytest.mark.parametrize('plan_first', [True, False])
@pytest.mark.parametrize('plan_case', ['original', 'invalid', 'short', 'superseded'])
def test_mixed_plan_and_aggregate_reach_completion_without_losing_plan(tmp_path: Path, plan_first: bool, plan_case: str) -> None:
    root = Path(__file__).resolve().parents[1]
    replay = json.loads((root / 'tests/fixtures/plan_projection_replay.json').read_bytes())
    prefix = replay['prefix']
    mixed = replay['mixed']
    mixed_calls = json.loads(mixed)
    if plan_case in {'invalid', 'short'}:
        mixed_calls['calls'][0]['arguments']['steps'] = [] if plan_case == 'invalid' else ['做']
        mixed = json.dumps(mixed_calls, ensure_ascii=False)
    if plan_case == 'superseded':
        old_plan = {'name': 'plan_update', 'arguments': {'steps': ['Earlier distinct plan']}}
        plan, aggregate = mixed_calls['calls']
        mixed_calls['calls'] = [old_plan, plan, aggregate] if plan_first else [aggregate, old_plan, plan]
        mixed = json.dumps(mixed_calls, ensure_ascii=False)
    elif not plan_first:
        mixed_calls['calls'].reverse()
        mixed = json.dumps(mixed_calls, ensure_ascii=False)
    draft = replay['draft']
    if plan_case == 'short':
        draft_data = json.loads(draft)
        draft_data['calls'][0]['arguments']['content'] += '\n' + ('x' * 600)
        draft = json.dumps(draft_data, ensure_ascii=False)
    elif plan_case == 'superseded':
        draft_data = json.loads(draft)
        draft_data['calls'][0]['arguments']['content'] = draft_data['calls'][0]['arguments']['content'].split('## 結論', 1)[0]
        draft = json.dumps(draft_data, ensure_ascii=False)
    replies = [httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {'role': 'assistant', 'content': text}}]})
               for text in [*prefix, mixed, draft]]
    replies.append(json_reply('task_finish', {}))
    peer = CompletionPeer(replies)
    name = 'qwen3.5:4b-q4_K_M'
    async def transport(request: httpx.Request) -> httpx.Response:
        if request.url.path == '/api/version': return httpx.Response(200, json={'version': '0.40.10'})
        if request.url.path == '/api/tags': return httpx.Response(200, json={'models': [{'name': name, 'digest': 'd8b0f5e9760cd1682034f292d7ef72ec46f432149be0df7574bf2d6e92e38c04'}]})
        if request.url.path == '/api/show': return httpx.Response(200, json=qwen_metadata())
        if len(peer.requests) == 5:
            body = json.loads(request.content)
            published = json.loads(body['messages'][-1]['content'])['tool_result']['value']
            peer.replies[5] = json_reply('task_finish', {'artifact_ids': [published['id']]})
        return await peer(request)
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'metrics.csv').write_bytes((root / 'evaluation/slice-09/analysis-missing/metrics.csv').read_bytes())
    suite = json.loads((root / 'evaluation/slice-09/cases.json').read_bytes())
    case = next(item for item in suite['cases'] if item['id'] == 'analysis-missing')
    settings = ModelSettings.local(name, thinking=False, stream=False, decision_protocol='json_tools')
    with TestClient(create_app(tmp_path / 'store', model_settings=settings, model_transport=httpx.MockTransport(transport))) as client:
        task = case['task'] + ('x' * 400 if plan_case == 'short' else '')
        run, events = finish(client, create_task(client, source, task=task, sources=case['sources'], limits=suite['limits'], completion_workflow='artifact_receipt'))
        if plan_case == 'invalid':
            assert any(e['kind'] == 'tool.rejected' and e['data'].get('tool') == 'plan_update' for e in events)
            assert not any(e['kind'] == 'context.pruned' and e['data'].get('reason') == 'duplicate_plan_encoding' for e in events)
            assert any(e['kind'] in {'context.pruned', 'context.limited'} for e in events)
            assert run['status'] == 'failed'
            return
        if plan_case == 'short':
            assert any(e['kind'] in {'context.pruned', 'context.limited'} for e in events)
            assert len(run['artifacts']) == 1
            assert not any(e['kind'] == 'context.pruned' and e['data'].get('reason') == 'duplicate_plan_encoding' for e in events)
            return
        assert run['status'] == 'completed', run['error']
        aggregate = next(e['data']['result'] for e in events if e['kind'] == 'tool.completed' and e['data']['tool'] == 'table_aggregate')
        assert aggregate['value'] == '150'
        plan = next(e['data']['steps'] for e in reversed(events) if e['kind'] == 'plan.updated')
        plan_index = (1 if plan_first else 2) if plan_case == 'superseded' else (0 if plan_first else 1)
        assert plan == json.loads(mixed)['calls'][plan_index]['arguments']['steps']
        assert any(e['kind'] == 'context.pruned' and e['data'].get('reason') == 'duplicate_plan_encoding' for e in events)
        sent = peer.requests[-1]['messages']
        plan_call = next(json.loads(message['content']) for message in sent if message['role'] == 'assistant'
                         and message.get('content') and 'plan_update' in message['content'])
        assert plan_call['calls'][plan_index]['arguments']['steps'] == plan
        assert any(call['name'] == 'table_aggregate' for call in plan_call['calls'])
        if plan_case == 'superseded':
            assert plan_call['calls'][plan_index - 1]['arguments']['steps'] == ['Earlier distinct plan']
            assert any(e['kind'] == 'plan.updated' and e['data']['steps'] == ['Earlier distinct plan'] for e in events)
        encoded = next(json.loads(message['content'])['tool_result'] for message in sent
                       if message['role'] == 'user' and f'"same_call_index":{plan_index}' in message.get('content', ''))
        assert encoded['name'] == 'plan_update' and encoded['value']['saved'] is True
        assert plan_call['calls'][encoded['value']['steps']['same_call_index']]['arguments']['steps'] == plan
        assert client.get(run['artifacts'][0]['download_url']).text.startswith(json.loads(draft)['calls'][0]['arguments']['content'])
        assert all(e['data']['context_budget']['upper_bound_tokens'] <= 8192 for e in events if e['kind'] == 'model.requested')


def test_encoded_plan_survives_process_restart(tmp_path: Path) -> None:
    from draft_process import finish_draft_run, running_draft_service
    from test_recovery import create_process_task, stop

    root = Path(__file__).resolve().parents[1]
    replay = json.loads((root / 'tests/fixtures/plan_projection_replay.json').read_bytes())
    prefix, mixed, draft = replay['prefix'], replay['mixed'], replay['draft']
    replies = [httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {'role': 'assistant', 'content': text}}]})
               for text in [*prefix, mixed, draft]]
    suite = json.loads((root / 'evaluation/slice-09/cases.json').read_bytes())
    case = next(item for item in suite['cases'] if item['id'] == 'analysis-missing')
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'metrics.csv').write_bytes((root / 'evaluation/slice-09/analysis-missing/metrics.csv').read_bytes())
    store = tmp_path / 'store'
    with running_draft_service(store, replies, decision_protocol='json_tools', qwen_4b=True) as first:
        original = create_process_task(first.client, source, task=case['task'], sources=case['sources'], limits=suite['limits'], completion_workflow='artifact_receipt')
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            events = first.client.get(f"/api/runs/{original['id']}/events").raise_for_status().json()['events']
            if any(e['kind'] == 'model.requested' and e['data'].get('step') == 6 for e in events):
                break
            time.sleep(.02)
        else:
            pytest.fail('Expected completion decision was not reached')
        assert any(e['kind'] == 'context.pruned' and e['data'].get('reason') == 'duplicate_plan_encoding' for e in events)
        saved = first.client.get(f"/api/runs/{original['id']}").raise_for_status().json()['artifacts'][0]
        stop(first.process)
    with running_draft_service(store, [json_reply('task_finish', {'artifact_ids': [saved['id']]})], decision_protocol='json_tools', qwen_4b=True) as second:
        checkpoint = second.client.get(f"/api/runs/{original['id']}/checkpoint").raise_for_status().json()
        plan = next(item['result']['steps'] for item in reversed(checkpoint['confirmed']) if item['tool'] == 'plan_update')
        assert plan == json.loads(mixed)['calls'][0]['arguments']['steps']
        assert next(item['result']['value'] for item in checkpoint['confirmed'] if item['tool'] == 'table_aggregate') == '150'
        resumed = second.client.post(f"/api/runs/{original['id']}/resume", json={'checkpoint_sha256': checkpoint['sha256'], 'submission_key': 'plan-projection-recovery'})
        assert resumed.status_code == 202, resumed.text
        final, events = finish_draft_run(second.client, resumed.json()['id'])
        assert final['status'] == 'completed', final['error']
        assert [item['id'] for item in final['inherited_artifacts']] == [saved['id']]
        assert [e['data']['tool'] for e in events if e['kind'] == 'tool.dispatched'] == ['task_finish']
        pruned = [e['data'].get('reason') for e in events if e['kind'] == 'context.pruned']
        assert pruned, 'Recovery test must actually exercise context pressure'
        assert not ({'labelled_lines_projection', 'tool_result_json_whitespace'} & set(pruned))
