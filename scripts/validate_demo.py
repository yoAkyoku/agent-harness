"""Opt-in six-case evidence recorder. One case per invocation; never invoked by pytest."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess
import time
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

from agent_harness.implementation import source_identity
from validate_local_model import save, workbench_headers

ROOT = Path(__file__).resolve().parents[1]
INSTALLED_MODEL_DIGESTS = {
    'qwen3.5:9b-q4_K_M': '56671c2ab9385f9cfcb404638e32cd62d88e3501d44822208363c010179a3c90',
    'qwen3.5:4b-q4_K_M': 'd8b0f5e9760cd1682034f292d7ef72ec46f432149be0df7574bf2d6e92e38c04',
}


def explicit_missing_document_fields(body: str, missing: list[str]) -> bool:
    """Require each missing required field to be marked in its own source row."""
    markers = re.compile(r'未提供|未註明|未載明|缺失|不詳|unknown|not provided|unspecified', re.I)
    aliases = {
        'owner': {'owner', '負責人', '負責者'},
        'decision': {'decision', '決議', '決策'},
        'date': {'date', '日期'},
    }
    lines = [line.strip() for line in body.splitlines()]
    for requirement in missing:
        try:
            filename, field = requirement.rsplit(' ', 1)
        except ValueError:
            return False
        field_names = aliases.get(field.casefold(), {field.casefold()})
        explicit = False
        for index, line in enumerate(lines):
            if not (line.startswith('|') and line.endswith('|')):
                continue
            headers = [cell.strip().strip('`').casefold() for cell in line[1:-1].split('|')]
            if not any(name in field_names for name in headers):
                continue
            for data_line in lines[index + 1:]:
                if not (data_line.startswith('|') and data_line.endswith('|')):
                    break
                cells = [cell.strip() for cell in data_line[1:-1].split('|')]
                if all(re.fullmatch(r':?-{3,}:?', cell) for cell in cells):
                    continue
                source_cell = next((cell for cell in cells if filename.casefold() in cell.casefold()), None)
                field_index = next((i for i, header in enumerate(headers) if header in field_names), None)
                if source_cell is not None and field_index is not None and field_index < len(cells):
                    explicit = bool(markers.search(cells[field_index]))
                    break
            if explicit:
                break
        if not explicit:
            # Support a labeled list while keeping source, field and marker together.
            explicit = any(
                filename.casefold() in line.casefold()
                and any(name in line.casefold() for name in field_names)
                and markers.search(line)
                for line in lines
            )
        if not explicit:
            return False
    return True


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--base-url', default='http://127.0.0.1:8801')
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--case', required=True)
    parser.add_argument('--model', choices=INSTALLED_MODEL_DIGESTS,
                        help='Explicit authorized comparison model; omitted uses the immutable suite default.')
    parser.add_argument('--publication-workflow', choices=('direct', 'draft_review'), default='direct',
                        help='Freeze the explicit publication policy for this entire batch.')
    parser.add_argument('--decision-protocol', choices=('native', 'json_tools'), default='native',
                        help='Freeze the explicit wire protocol; the service must match.')
    parser.add_argument('--completion-workflow', choices=('model_summary', 'artifact_receipt'), default='model_summary',
                        help='Freeze how the model selects final completion for this batch.')
    parser.add_argument('--suite', type=Path, default=ROOT / 'evaluation/slice-09/cases.json',
                        help='Predeclared case suite; sources live in <suite dir>/<case id>/.')
    parser.add_argument('--observe-all', action='store_true',
                        help='Quality-observation batch: every case still needs a saved manual review before the next, '
                             'but a rejected case does not stop the batch (slice 09 spec, 2026-11-08).')
    parser.add_argument('--allow-dirty-candidate', action='store_true',
                        help='Allow a local candidate only when the service, package, and recorder fingerprints stay fixed.')
    args = parser.parse_args()
    target = urlsplit(args.base_url)
    if target.scheme != 'http' or target.hostname != '127.0.0.1' or target.path not in {'', '/'} or target.username or target.password or target.query or target.fragment:
        parser.error('Use only the explicitly started loopback workbench.')
    suite_path = args.suite.resolve()
    suite_directory = suite_path.parent
    suite = json.loads(suite_path.read_text(encoding='utf-8'))
    selected_model = args.model or suite['model']
    case = next((item for item in suite['cases'] if item['id'] == args.case), None)
    if case is None:
        parser.error(f'Choose a predeclared case ID from {suite_path}.')
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    implementation_status = subprocess.check_output(['git', 'status', '--porcelain', '--', 'src', 'scripts', 'evaluation', 'pyproject.toml', 'uv.lock'], cwd=ROOT, text=True)
    candidate_source_identity = source_identity(ROOT / 'src' / 'agent_harness')
    recorder_sha256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    if implementation_status and not args.allow_dirty_candidate:
        parser.error('Commit implementation/input changes before starting a fixed quality batch, or opt into a fingerprinted local candidate.')
    suite_hash = hashlib.sha256(suite_path.read_bytes()).hexdigest()
    input_sources = {item['id']: {name: (suite_directory/item['id']/name).read_text(encoding='utf-8') for name in item['sources']} for item in suite['cases']}
    with httpx.Client(base_url=args.base_url, trust_env=False, timeout=15, headers=workbench_headers(),
                      limits=httpx.Limits(max_keepalive_connections=0)) as client:
        config = client.get('/api/config').raise_for_status().json()
        assert config.get('implementation') == source_identity(ROOT / 'src' / 'agent_harness'), 'Service implementation differs or is unidentified; restart this checkout before admitting a validation Run.'
        assert config['model']['name'] == selected_model and config['model']['transport_fixture'] is False, 'Service does not match the explicitly selected model.'
        assert config['model']['digest'] == INSTALLED_MODEL_DIGESTS[selected_model]
        assert config['model'].get('decision_protocol', 'native') == args.decision_protocol, 'Service decision protocol differs from this batch selection.'
        assert not config['validation'].get('fault_once_source_read') and not config['validation'].get('hold_first_tool')
        manifest_path = output / 'manifest.json'
        manifest = {'started_at': datetime.now(timezone.utc).isoformat(), 'git_revision': revision,
                    'implementation_status': implementation_status,
                    'candidate_fingerprints': {'implementation_source': candidate_source_identity,
                                               'recorder_sha256': recorder_sha256},
                    'config': config,
                    'publication_workflow': args.publication_workflow,
                    'decision_protocol': args.decision_protocol,
                    'completion_workflow': args.completion_workflow,
                    'model_selection': {'selected': selected_model, 'suite_default': suite['model'], 'comparison': selected_model != suite['model']},
                    'batch_mode': 'quality_observation' if args.observe_all else 'acceptance_gate',
                    'suite_sha256': suite_hash, 'suite': suite, 'input_sources': input_sources, 'base_url': args.base_url,
                    'policy': 'one admitted Run per case; retain failures; manually review each case before the next; no fallback/download'}
        if manifest_path.exists():
            original = json.loads(manifest_path.read_text(encoding='utf-8'))
            assert original.get('publication_workflow', 'direct') == args.publication_workflow, 'Create a new batch after publication workflow changes.'
            assert original.get('decision_protocol', 'native') == args.decision_protocol, 'Create a new batch after decision protocol changes.'
            assert original.get('completion_workflow', 'model_summary') == args.completion_workflow, 'Create a new batch after completion workflow changes.'
            assert (original['git_revision'] == revision and original['suite_sha256'] == suite_hash
                    and original['input_sources'] == input_sources and original['config'] == config
                    and original.get('implementation_status', '') == implementation_status
                    and original.get('candidate_fingerprints') == manifest['candidate_fingerprints']), \
                'Create a new batch after implementation, input, recorder, or configuration changes.'
        else:
            save(manifest_path, manifest)
        index = suite['cases'].index(case)
        for previous in suite['cases'][:index]:
            review = output / previous['id'] / 'manual-review.json'
            assert review.is_file(), 'Review each preceding case before advancing.'
            if not args.observe_all:
                assert json.loads(review.read_text(encoding='utf-8')).get('accepted') is True, 'Review each preceding case before advancing; failed batches remain incomplete.'
        directory = output / case['id']
        directory.mkdir(exist_ok=True)
        if (directory / 'assessment.json').exists():
            print('Existing assessment retained; no model call or resubmission.', flush=True)
            return
        source_directory = suite_directory / case['id']
        sources = [{'path': name, 'sha256': hashlib.sha256((source_directory/name).read_bytes()).hexdigest(), 'bytes': (source_directory/name).stat().st_size} for name in case['sources']]
        if (directory / 'admission.json').exists():
            admission = json.loads((directory/'admission.json').read_text(encoding='utf-8'))
            run = client.get(f"/api/runs/{admission['id']}").raise_for_status().json()
            print(f"Observing existing {run['id']}", flush=True)
        else:
            assert not (directory/'request.json').exists(), 'Unknown admission outcome: inspect saved submission key in the workbench; do not create another task.'
            workspace = client.post('/api/workspaces', json={'path': str(source_directory)}).raise_for_status().json()
            session = client.post('/api/sessions', json={'workspace_id': workspace['id'], 'title': case['id']}).raise_for_status().json()
            request = {'task': case['task'], 'sources': case['sources'], 'limits': suite['limits'], 'require_publish_approval': False, 'publication_workflow': args.publication_workflow, 'submission_key': str(uuid4())}
            if args.completion_workflow != 'model_summary':
                request['completion_workflow'] = args.completion_workflow
            save(directory/'source-hashes.json', sources)
            save(directory/'session.json', session)
            save(directory/'request.json', request)
            run = client.post(f"/api/sessions/{session['id']}/runs", json=request).raise_for_status().json()
            save(directory/'admission.json', run)
            print(f"Admitted {case['id']}: {run['id']}", flush=True)
        started = time.monotonic()
        while run['status'] in {'queued', 'running', 'waiting_approval', 'cancel_requested'}:
            if time.monotonic()-started > 195:
                raise RuntimeError('Observer deadline exceeded; inspect this saved Run without resubmitting.')
            time.sleep(.5)
            run = client.get(f"/api/runs/{run['id']}").raise_for_status().json()
        events = client.get(f"/api/runs/{run['id']}/events").raise_for_status().json()['events']
        save(directory/'run.json', run)
        save(directory/'events.json', events)
        with httpx.Client(trust_env=False, timeout=10) as provider:
            save(directory/'loaded-model.json', provider.get('http://127.0.0.1:11434/api/ps').raise_for_status().json())
        bodies = []
        full_texts = []
        artifact_exports = []
        for artifact in run['artifacts']:
            raw = client.get(artifact['download_url']).raise_for_status().content
            assert hashlib.sha256(raw).hexdigest() == artifact['sha256']
            artifact_directory = directory/'artifacts'/artifact['id']
            artifact_directory.mkdir(parents=True, exist_ok=True)
            artifact_path = artifact_directory/artifact['name']
            if artifact_path.exists():
                assert artifact_path.read_bytes() == raw, 'Previously saved artifact changed; retain the original evidence.'
            else:
                artifact_path.write_bytes(raw)
            artifact_exports.append({'id': artifact['id'], 'name': artifact['name'], 'sha256': artifact['sha256'],
                                     'bytes': len(raw), 'export_path': artifact_path.relative_to(directory).as_posix()})
            full_texts.append(raw.decode('utf-8'))
            bodies.append(full_texts[-1].split('\n\n## Verified tool evidence', 1)[0])
        save(directory/'artifact-manifest.json', artifact_exports)
        body = '\n'.join(bodies)
        full_text = '\n'.join(full_texts)
        results = [event['data']['result'] for event in events if event['kind'] == 'tool.completed']
        tools = [event['data']['tool'] for event in events if event['kind'] == 'tool.completed']
        checks = {'completed': run['status'] == 'completed', 'artifact': bool(bodies),
                  'visible_plan': any(event['kind'] == 'plan.updated' for event in events),
                  'all_selected_sources_cited': all(source['sha256'] in full_text for source in sources),
                  'source_versions': sorted(run['sources'],key=lambda x:x['path']) == sorted(sources,key=lambda x:x['path']),
                  'source_reads': 'source_read' in tools or 'table_inspect' in tools,
                  'citations': bool(bodies) and all('## Verified tool evidence' in text for text in full_texts)}
        if case['kind'] == 'analysis' and 'expected_values' in case['truth']:
            # Generic analysis truth: every expected value appears in the body and
            # every numeric result was produced by a tool.
            truth = case['truth']
            checks['numeric_tool_oracle'] = all(value in [result.get('value') for result in results] for value in truth['numeric_results'])
            checks['calculation_path'] = all(tool in tools for tool in truth.get('required_tools', []))
            for label, value in truth['expected_values'].items():
                checks[f'body_value:{label}'] = bool(re.search(r'(?<![\d.])' + re.escape(value) + r'(?:\.0+)?(?![\d])', body))
        elif case['kind'] == 'analysis':
            truth = case['truth']
            checks['numeric_tool_oracle'] = all(value in [result.get('value') for result in results] for value in truth['numeric_results'])
            checks['orders_body'] = bool(re.search(r'(?<!\d)'+truth['orders']+r'(?!\d)', body))
            if truth['rate'] is not None:
                checks['calculation_path'] = 'table_aggregate' in tools and 'number_calculate' in tools
                checks['exceptions_body'] = bool(re.search(r'(?<!\d)'+truth['exceptions']+r'(?!\d)', body))
                checks['rate_body'] = bool(re.search(re.escape(truth['rate'])+r'0*\s*(?:\*\*)?\s*%', body))
            else:
                checks['missing_reported'] = bool(re.search(r'缺|未提供|無法|missing|unavailable', body, re.I))
                checks['no_invented_rate'] = not re.search(r'\d(?:\.\d+)?\s*(?:\*\*)?\s*%',body)
        else:
            checks['filenames_in_body'] = all(name in body for name in case['sources'])
            people = [word for facts in case['truth']['facts'].values() for word in facts if re.fullmatch('[A-Z][a-z]+',word)]
            checks['named_people_in_body'] = all(person in body for person in people)
            checks['document_tool_path'] = 'source_read' in tools and 'table_aggregate' not in tools
            if case['truth']['missing']:
                checks['missing_field_values_explicit'] = explicit_missing_document_fields(
                    body, case['truth']['missing'])
        elapsed = (datetime.fromisoformat(events[-1]['timestamp'])-datetime.fromisoformat(run['created_at'])).total_seconds()
        save(directory/'assessment.json', {'checks': checks, 'tool_path': tools, 'elapsed_seconds': round(elapsed,2), 'usage': run['usage'],
            'manual_review_required': True, 'quality_accepted': False,
            'manual_rubric': 'Review model-written body, final reply, plan and every visible text against source truth; verify labels, numeric relations, omissions, owner/topic/fact associations and absence of fabricated dates or missing-data substitutes. Tool appendix and lexical checks alone never prove quality.'})
        print(f"{run['status']}: {checks}", flush=True)
        if not all(checks.values()):
            if args.observe_all:
                raise SystemExit('Automatic checks failed; evidence retained. Do not rerun this case; record its manual review, then continue.')
            raise SystemExit('Failed batch retained. Do not rerun the case or advance; diagnose and create a new batch after an explicit change.')


if __name__ == '__main__':
    main()
