"""Fast contracts for the real-QMD final calibration harness."""
import importlib.util
from pathlib import Path
import sys

import pytest

from tests.plugins.kb_test_support import (
    QmdFixtureError, QmdFixtureUnconfigured, resolve_qmd_fixture,
)

BENCHMARK = Path(__file__).with_name('kb_final_benchmark.py')


def module():
    spec = importlib.util.spec_from_file_location('kb_final_benchmark_contract', BENCHMARK)
    loaded = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = loaded
    spec.loader.exec_module(loaded)
    return loaded


def test_explicit_qmd_fixture_contract(monkeypatch, tmp_path):
    monkeypatch.delenv('QMD_TEST_ROOT', raising=False)
    with pytest.raises(QmdFixtureUnconfigured, match='QMD_TEST_ROOT'):
        resolve_qmd_fixture()

    monkeypatch.setenv('QMD_TEST_ROOT', str(tmp_path.resolve()))
    with pytest.raises(QmdFixtureError, match='missing required assets'):
        resolve_qmd_fixture()


def test_benchmark_has_reviewed_corpus_and_query_cardinality(tmp_path):
    benchmark = module()
    vault, expected = benchmark.build_corpus(tmp_path / 'synthetic-only', 180)
    assert len(list(vault.rglob('*.md'))) == 180
    assert len(benchmark.FACTS) == 15
    assert len(benchmark.NEGATIVE_QUERIES) == 15
    assert len({row[2] for row in benchmark.FACTS[:5]}) == 5
    assert set(path.parts[-3] if 'profiles' in path.parts else 'shared'
               for path in vault.rglob('fact-*.md')) == {'shared', 'profiles'}
    assert len(expected) == 15


def test_threshold_sweep_reuses_rows_without_search_calls():
    benchmark = module()

    class Multiscope:
        @staticmethod
        def _select_records(candidates, limit, _budget):
            rows = [row for values in candidates.values() for row in values]
            return sorted(rows, key=lambda row: -row['_score'])[:limit]

    class API:
        kb_multiscope = Multiscope()

    positive = {'kind': 'positive', 'expected_uri': 'qmd://shared/fact.md',
                'semantic_rows': {'shared': [{'file': 'qmd://shared/fact.md', 'score': .3, 'lexical_hit': False}]},
                'lexical_rows': {'shared': []}, 'semantic_latency_ms': 10}
    negative = {'kind': 'negative',
                'semantic_rows': {'shared': [{'file': 'qmd://shared/noise.md', 'score': .2, 'lexical_hit': False}]},
                'lexical_rows': {'shared': []}, 'semantic_latency_ms': 20}
    exact = {'kind': 'exact_id', 'expected_uri': 'qmd://shared/id.md',
             'semantic_rows': {'shared': [{'file': 'qmd://shared/id.md', 'score': 0, 'lexical_hit': False}]},
             'lexical_rows': {}, 'semantic_latency_ms': 5}
    metrics = benchmark.evaluate(API(), [positive, negative, exact], [.15, .25, .35])
    assert [row['recall_at_3'] for row in metrics['threshold_sweep']] == [1, 1, 0]
    assert [row['false_injection_rate'] for row in metrics['threshold_sweep']] == [1, 0, 0]
    assert metrics['exact_id_recall_at_3'] == 1
