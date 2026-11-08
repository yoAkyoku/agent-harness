"""Public HTTP/model transport control for plan encoding with prior Session history."""
import json
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from agent_harness_demo.model import ModelSettings
from agent_harness_demo.web import create_app
from test_completion import CompletionPeer
from test_json_decisions import json_reply
from test_session_versions import qwen_metadata
from test_workbench import create_task, finish


def test_prior_turn_is_pruned_before_current_mixed_plan_encoding(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    fixture = json.loads((root / 'tests/fixtures/plan_projection_replay.json').read_bytes())
    mixed = json.loads(fixture['mixed'])
    plan = mixed['calls'][0]['arguments']['steps']
    first = [json_reply('plan_update', {'steps': plan}), json_reply('table_inspect', {'path': 'metrics.csv'}),
             json_reply('table_aggregate', {'path': 'metrics.csv', 'column': 'orders', 'operation': 'sum'}),
             json_reply('artifact_write', {'name': 'old.md', 'content': 'Total 150 [E2]', 'citations': ['E1', 'E2']}),
             json_reply('task_finish', {})]
    compact_draft = json.loads(fixture['draft'])
    compact_draft['calls'][0]['arguments']['content'] = '# 報告\n訂單總數 150 [E2]；異常總數及異常率無法計算 [E1]。'
    later = [httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {'role': 'assistant', 'content': text}}]})
             for text in [*fixture['prefix'], fixture['mixed'], json.dumps(compact_draft, ensure_ascii=False)]] + [json_reply('task_finish', {})]
    peer = CompletionPeer(first + later)
    name = 'qwen3.5:4b-q4_K_M'
    async def transport(request: httpx.Request) -> httpx.Response:
        if request.url.path == '/api/version': return httpx.Response(200, json={'version': '0.40.10'})
        if request.url.path == '/api/tags': return httpx.Response(200, json={'models': [{'name': name, 'digest': 'd8b0f5e9760cd1682034f292d7ef72ec46f432149be0df7574bf2d6e92e38c04'}]})
        if request.url.path == '/api/show': return httpx.Response(200, json=qwen_metadata())
        if len(peer.requests) in {4, 10}:
            body = json.loads(request.content)
            published = json.loads(body['messages'][-1]['content'])['tool_result']['value']
            peer.replies[len(peer.requests)] = json_reply('task_finish', {'artifact_ids': [published['id']]})
        return await peer(request)
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'metrics.csv').write_bytes((root / 'evaluation/slice-09/analysis-missing/metrics.csv').read_bytes())
    suite = json.loads((root / 'evaluation/slice-09/cases.json').read_bytes())
    case = next(item for item in suite['cases'] if item['id'] == 'analysis-missing')
    settings = ModelSettings.local(name, thinking=False, stream=False, decision_protocol='json_tools')
    with TestClient(create_app(tmp_path / 'store', model_settings=settings, model_transport=httpx.MockTransport(transport))) as client:
        first_run, _ = finish(client, create_task(client, source, task='Summarize the order count.', sources=['metrics.csv'], limits=suite['limits'], completion_workflow='artifact_receipt'))
        assert first_run['status'] == 'completed', first_run['error']
        base = first_run['artifacts'][0]
        response = client.post(f"/api/sessions/{first_run['session_id']}/runs", json={
            'task': case['task'], 'sources': case['sources'], 'limits': suite['limits'],
            'completion_workflow': 'artifact_receipt', 'base_artifact': {'run_id': first_run['id'], 'artifact_id': base['id']},
        })
        assert response.status_code == 202, response.text
        second, events = finish(client, response.json())
        assert second['status'] == 'completed', (second['error'], [(e['kind'], e['data'].get('step'), e['data'].get('reason'), e['data'].get('upper_bound_tokens')) for e in events if e['kind'] in {'context.pruned', 'context.limited'}])
        selected = next(e for e in events if e['kind'] == 'context.selected')
        assert first_run['id'] in selected['data']['history_run_ids']
        history_pruned = next(e for e in events if e['kind'] == 'context.pruned' and e['data'].get('history_run_id') == first_run['id'])
        encoded = next(e for e in events if e['kind'] == 'context.pruned' and e['data'].get('reason') == 'duplicate_plan_encoding')
        assert history_pruned['sequence'] < encoded['sequence']
        sent = peer.requests[-1]['messages']
        plan_call = next(json.loads(m['content']) for m in sent if m['role'] == 'assistant' and 'plan_update' in m.get('content', ''))
        assert plan_call['calls'][0]['arguments']['steps'] == plan
        assert any('"same_call_index":0' in m.get('content', '') for m in sent)
