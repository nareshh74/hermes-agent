"""Real-QMD synthetic calibration benchmark for two-level KB retrieval.

This reads only generated notes under --output, runs each semantic/lexical query once,
and performs threshold sweeps offline from the stored real scores.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import statistics
import sys
import time

SOURCE = Path(__file__).parents[2] / 'standalone-kb'
REPO = SOURCE.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tests.plugins.kb_test_support import QmdFixture, resolve_qmd_fixture

FACTS = (
    ('shared', 'atlas', 'KBX-100', 'Project Atlas deploys updated APIs with gradual canary traffic shifting before global cutover.',
     'How can Atlas release an updated API while avoiding an outage?'),
    ('shared', 'orion', 'KBX-101', 'Incident Orion changes the primary pager rotation every Wednesday morning.',
     'On which day does the main on-call assignment for Orion rotate?'),
    ('shared', 'juniper', 'KBX-102', 'Build Juniper stores reusable Python wheel artifacts in an Azure blob cache.',
     'Where does Juniper keep prebuilt Python package artifacts?'),
    ('shared', 'lantern', 'KBX-103', 'Service Lantern encrypts recovery backups with key version seven.',
     'Which encryption key generation protects Lantern restore copies?'),
    ('shared', 'meridian', 'KBX-104', 'Dashboard Meridian tracks the ninety-fifth percentile of queue latency.',
     'Which queue delay percentile is monitored by Meridian?'),
    ('alpha', 'redwood', 'KBX-200', 'Client Redwood performs regional failover with weighted DNS records.',
     'What routing mechanism moves Redwood traffic between regions during failover?'),
    ('alpha', 'saffron', 'KBX-201', 'Feature Saffron begins rollout with ring three pilot tenants.',
     'Which early customer cohort receives Saffron first?'),
    ('alpha', 'cedar', 'KBX-202', 'Dataset Cedar retains operational events for forty-five days.',
     'How long are Cedar operational records kept?'),
    ('alpha', 'quartz', 'KBX-203', 'Migration Quartz maintains dual writes while the new store catches up.',
     'How does Quartz keep old and new storage synchronized during migration?'),
    ('alpha', 'harbor', 'KBX-204', 'Rollout Harbor automatically rolls back when errors exceed two percent.',
     'What error rate causes Harbor to reverse a deployment?'),
    ('beta', 'nimbus', 'KBX-300', 'Nimbus background workers hold task leases for ninety seconds.',
     'How long may a Nimbus worker own a job before its lease expires?'),
    ('beta', 'violet', 'KBX-301', 'The Violet API permits two hundred forty requests per minute.',
     'What is Violet’s per-minute request allowance?'),
    ('beta', 'copper', 'KBX-302', 'Copper database queries use a composite index on tenant and creation time.',
     'Which two fields form the Copper query index?'),
    ('beta', 'maple', 'KBX-303', 'Maple sends jobs to the dead-letter queue after five failed attempts.',
     'How many unsuccessful tries occur before Maple quarantines a job?'),
    ('beta', 'polar', 'KBX-304', 'Polar authentication tokens expire after eighteen minutes.',
     'What is the lifetime of a Polar login token?'),
)

NEGATIVE_QUERIES = (
    'What propellant mixture powers a lunar transfer stage?',
    'Who won the international chess championship?',
    'How should sourdough starter be fed overnight?',
    'What is the rainfall forecast for coastal Kerala?',
    'Which museum displays the oldest Egyptian papyrus?',
    'How do I replace a bicycle hydraulic brake line?',
    'What dosage of vitamin D is recommended for infants?',
    'Which train runs between Kyoto and Sapporo?',
    'What chord progression opens a baroque fugue?',
    'How does photosynthesis differ in desert cactus plants?',
    'When is the next total solar eclipse visible in Brazil?',
    'What fabric is best for a winter climbing jacket?',
    'How do migratory whales navigate across oceans?',
    'Which telescope discovered the first known exoplanet?',
    'What temperature produces a crisp sourdough crust?',
)

DISTRACTOR_TEMPLATES = (
    'The {name} service publishes weekly capacity summaries for the {team} team.',
    'Project {name} validates routine maintenance checklists in the {region} region.',
    'The {name} dashboard archives monthly availability reports for {team}.',
    'System {name} uses standard approval records for planned maintenance in {region}.',
    'The {name} component documents ownership contacts and quarterly review dates for {team}.',
)


def load_plugin():
    name = 'kb_benchmark_plugin'
    for module in list(sys.modules):
        if module == name or module.startswith(name + '.'):
            del sys.modules[module]
    spec = importlib.util.spec_from_file_location(name, SOURCE / '__init__.py', submodule_search_locations=[str(SOURCE)])
    plugin = importlib.util.module_from_spec(spec)
    sys.modules[name] = plugin
    spec.loader.exec_module(plugin)
    return plugin


def model_settings(fixture: QmdFixture):
    verified = json.loads(fixture.models.read_text(encoding='utf-8'))
    by_name = {Path(item['path']).name: item for item in verified}
    roles = {
        'embed': by_name['embeddinggemma-300M-Q8_0.gguf'],
        'generate': by_name['qmd-query-expansion-1.7B-q4_k_m.gguf'],
        'rerank': by_name['qwen3-reranker-0.6b-q8_0.gguf'],
    }
    missing = [item['path'] for item in roles.values()
               if not Path(item['path']).is_absolute() or not Path(item['path']).is_file()]
    if missing:
        raise FileNotFoundError(
            'configured QMD_TEST_ROOT model manifest references missing assets: ' + ', '.join(missing)
        )
    return ({role: item['path'] for role, item in roles.items()},
            {role: {'sha256': item['sha256'], 'bytes': item['bytes']} for role, item in roles.items()})


def build_corpus(root: Path, count: int = 180):
    if count < 150 or count > 300:
        raise ValueError('note count must stay within the reviewed 150..300 range')
    if root.exists():
        shutil.rmtree(root)
    vault = root / 'vault'
    for scope in ('shared', 'alpha', 'beta'):
        folder = vault / ('shared' if scope == 'shared' else f'profiles/{scope}')
        folder.mkdir(parents=True)
    expected = {}
    for scope, slug, identifier, fact, query in FACTS:
        folder = vault / ('shared' if scope == 'shared' else f'profiles/{scope}')
        path = folder / f'fact-{slug}.md'
        path.write_text(f'# {slug.title()} operating note\n{identifier}: {fact}\n', encoding='utf-8')
        expected[(scope, slug)] = path.name
    per_scope = count // 3
    fact_counts = {scope: sum(row[0] == scope for row in FACTS) for scope in ('shared', 'alpha', 'beta')}
    teams = ('payments', 'identity', 'storage', 'routing', 'analytics')
    regions = ('north', 'south', 'east', 'west', 'central')
    for scope in ('shared', 'alpha', 'beta'):
        folder = vault / ('shared' if scope == 'shared' else f'profiles/{scope}')
        for number in range(per_scope - fact_counts[scope]):
            name = f'{scope}-routine-{number:03d}'
            text = DISTRACTOR_TEMPLATES[number % len(DISTRACTOR_TEMPLATES)].format(
                name=name, team=teams[number % len(teams)], region=regions[(number * 3) % len(regions)])
            (folder / f'distractor-{number:03d}.md').write_text(
                f'# Routine note {number:03d}\n{text}\n', encoding='utf-8')
    total = len(list(vault.rglob('*.md')))
    if total != count:
        raise AssertionError(f'generated {total} notes, expected {count}')
    return vault, expected


def values(vault: Path, profile: str, mode: str, fixture: QmdFixture):
    models, provenance = model_settings(fixture)
    shared_index = {'qmd_command': [fixture.bun, str(fixture.qmd)], 'models': models,
                    'model_provenance': provenance, 'max_note_bytes': 1024 * 1024}
    return {
        'enabled': True, 'layout': 'two_level', 'profile_id': profile,
        'vault_root': str(vault), 'shared_writer_profile': 'default',
        'qmd_command': [fixture.bun, str(fixture.qmd)], 'shared_index': shared_index,
        'mode': mode, 'rerank_enabled': False,
        'models': models, 'model_provenance': provenance,
        'top_k': 3, 'max_context_chars': 6000, 'timeout_seconds': 10,
        'refresh_interval_seconds': 86400, 'min_score': 0,
    }


def query_rows(api, configs, query: str):
    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = {scope: pool.submit(api.search, cfg, query) for scope, cfg in configs.items()}
        rows = {scope: future.result() for scope, future in futures.items()}
    return rows, (time.perf_counter() - started) * 1000


def normalized_rows(rows):
    return {scope: [{'file': row.get('file'), 'score': row.get('score'),
                     'lexical_hit': bool(row.get('_lexical_hit'))} for row in scope_rows]
            for scope, scope_rows in rows.items()}


def selected_files(api, rows, threshold: float, *, semantic: bool):
    candidates = {}
    for scope, scope_rows in rows.items():
        candidates[scope] = [
            {'source': row['file'], 'scope': scope, '_score': float(row.get('score', -1))}
            for row in scope_rows
            if not semantic or row.get('_lexical_hit') or float(row.get('score', -1)) >= threshold
        ]
    return [record['source'] for record in api.kb_multiscope._select_records(candidates, 3, 100_000)]


def percentile(values, fraction):
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, int((len(ordered) - 1) * fraction)))]


def evaluate(api, cases, thresholds):
    sweep = []
    for threshold in thresholds:
        positives = [case for case in cases if case['kind'] == 'positive']
        negatives = [case for case in cases if case['kind'] == 'negative']
        hits = sum(case['expected_uri'] in selected_files(api, case['semantic_rows'], threshold, semantic=True)
                   for case in positives)
        false = sum(bool(selected_files(api, case['semantic_rows'], threshold, semantic=True))
                    for case in negatives)
        sweep.append({'threshold': threshold, 'recall_at_3': hits / len(positives),
                      'false_injection_rate': false / len(negatives),
                      'positive_hits': hits, 'positive_total': len(positives),
                      'negative_injections': false, 'negative_total': len(negatives)})
    baseline_positive = [case for case in cases if case['kind'] == 'positive']
    baseline_negative = [case for case in cases if case['kind'] == 'negative']
    lexical_hits = sum(case['expected_uri'] in selected_files(api, case['lexical_rows'], 0, semantic=False)
                       for case in baseline_positive)
    lexical_false = sum(bool(selected_files(api, case['lexical_rows'], 0, semantic=False))
                        for case in baseline_negative)
    exact = [case for case in cases if case['kind'] == 'exact_id']
    exact_hits = sum(case['expected_uri'] in selected_files(api, case['semantic_rows'], 0, semantic=False)
                     for case in exact)
    latencies = [case['semantic_latency_ms'] for case in cases]
    return {
        'threshold_sweep': sweep,
        'lexical_baseline': {'recall_at_3': lexical_hits / len(baseline_positive),
                             'false_injection_rate': lexical_false / len(baseline_negative)},
        'exact_id_recall_at_3': exact_hits / len(exact),
        'semantic_latency_ms': {'p50': statistics.median(latencies), 'p95': percentile(latencies, .95)},
    }


def run(root: Path, note_count: int, thresholds):
    fixture = resolve_qmd_fixture(require_models=True)
    api = load_plugin()
    vault, expected = build_corpus(root, note_count)
    system = root / 'home'
    homes = {'alpha': system / 'profiles/alpha', 'beta': system / 'profiles/beta'}
    semantic_configs = {
        profile: api.kb_multiscope.build_scopes(
            home, values(vault, profile, 'semantic', fixture)
        )
        for profile, home in homes.items()
    }
    refreshed = set()
    for profile, configs in semantic_configs.items():
        for scope, cfg in configs.items():
            state_key = str(cfg.state)
            if state_key not in refreshed:
                api.refresh(cfg, timeout=120)
                refreshed.add(state_key)
    lexical_configs = {
        profile: api.kb_multiscope.build_scopes(
            home, values(vault, profile, 'lexical', fixture)
        )
        for profile, home in homes.items()
    }
    cases = []
    for scope, slug, identifier, fact, query in FACTS:
        profile = scope if scope in homes else 'alpha'
        expected_scope = 'shared' if scope == 'shared' else 'private'
        collection = 'shared' if scope == 'shared' else 'profile-' + profile
        expected_uri = f'qmd://{collection}/{expected[(scope, slug)]}'
        semantic_rows, semantic_ms = query_rows(api, semantic_configs[profile], query)
        lexical_rows, lexical_ms = query_rows(api, lexical_configs[profile], query)
        cases.append({'kind': 'positive', 'profile': profile, 'query': query, 'expected_scope': expected_scope,
                      'expected_uri': expected_uri, 'semantic_rows': normalized_rows(semantic_rows),
                      'lexical_rows': normalized_rows(lexical_rows), 'semantic_latency_ms': semantic_ms,
                      'lexical_latency_ms': lexical_ms})
    for number, query in enumerate(NEGATIVE_QUERIES):
        profile = 'alpha' if number % 2 == 0 else 'beta'
        semantic_rows, semantic_ms = query_rows(api, semantic_configs[profile], query)
        lexical_rows, lexical_ms = query_rows(api, lexical_configs[profile], query)
        cases.append({'kind': 'negative', 'profile': profile, 'query': query,
                      'semantic_rows': normalized_rows(semantic_rows), 'lexical_rows': normalized_rows(lexical_rows),
                      'semantic_latency_ms': semantic_ms, 'lexical_latency_ms': lexical_ms})
    for scope, slug, identifier, fact, query in FACTS[:5]:
        profile = 'alpha'
        expected_uri = f'qmd://shared/{expected[(scope, slug)]}'
        semantic_rows, semantic_ms = query_rows(api, semantic_configs[profile], identifier)
        cases.append({'kind': 'exact_id', 'profile': profile, 'query': identifier, 'expected_uri': expected_uri,
                      'semantic_rows': normalized_rows(semantic_rows), 'lexical_rows': normalized_rows(semantic_rows),
                      'semantic_latency_ms': semantic_ms, 'lexical_latency_ms': semantic_ms})
    source_hashes = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                     for path in SOURCE.glob('*.py')}
    raw = {'schema': 1, 'note_count': note_count, 'query_counts': {'positive': 15, 'negative': 15, 'exact_id': 5},
           'source_hashes': source_hashes, 'cases': cases}
    (root / 'real-scores.json').write_text(json.dumps(raw, indent=2, ensure_ascii=False), encoding='utf-8')
    report = {'schema': 1, 'status': 'measured', 'note_count': note_count,
              'query_counts': raw['query_counts'], 'source_hashes': source_hashes,
              'metrics': evaluate(api, cases, thresholds),
              'calibration_limit': ('Synthetic generated notes are not a proxy for the future production vault. '
                                    'Re-run a held-out, consented corpus calibration before changing defaults.'),
              'models': json.loads(fixture.models.read_text(encoding='utf-8'))}
    (root / 'benchmark-report.json').write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding='utf-8')
    return report


def reevaluate_stored_scores(root: Path, thresholds):
    """Re-sweep stored real scores and refresh source hashes without invoking QMD."""
    fixture = resolve_qmd_fixture(require_models=True)
    raw = json.loads((root / 'real-scores.json').read_text(encoding='utf-8'))
    api = load_plugin()
    source_hashes = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                     for path in SOURCE.glob('*.py')}
    report = {'schema': 1, 'status': 'measured', 'note_count': raw['note_count'],
              'query_counts': raw['query_counts'], 'source_hashes': source_hashes,
              'metrics': evaluate(api, raw['cases'], thresholds),
              'calibration_limit': ('Synthetic generated notes are not a proxy for the future production vault. '
                                    'Re-run a held-out, consented corpus calibration before changing defaults.'),
              'models': json.loads(fixture.models.read_text(encoding='utf-8')),
              'score_source': 'stored real QMD rows; no model calls during this re-evaluation'}
    (root / 'benchmark-report.json').write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding='utf-8')
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--notes', type=int, default=180)
    parser.add_argument('--thresholds', default='0.15,0.19,0.23,0.27,0.31,0.35')
    parser.add_argument('--offline', action='store_true', help='re-sweep stored real scores; never invoke QMD')
    args = parser.parse_args()
    thresholds = [float(value) for value in args.thresholds.split(',')]
    report = reevaluate_stored_scores(args.output, thresholds) if args.offline else run(args.output, args.notes, thresholds)
    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == '__main__':
    main()
