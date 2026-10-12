"""QMD argv adapter with profile-contained state and bounded subprocesses."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re

import tempfile
import time

from .kb_core import ensure_state, lexical_terms, query_identifier
from .kb_process import ProcessScope
import subprocess

MAX_OUTPUT = 512_000
_SEMANTIC_NEGATION = re.compile(r'(?:^| )-+[A-Za-z0-9_"]')


class QmdExitError(RuntimeError):
    """A redacted QMD process failure carrying only its integer exit code."""

    def __init__(self, exit_code: int, *_discarded) -> None:
        self.exit_code = int(exit_code)
        super().__init__('QMD process failed')


def _quoted_lexical_query(text: str) -> str:
    quoted = []
    for term in lexical_terms(text):
        normalized = ' '.join(re.sub(r'[-_]', ' ', term).split())
        if normalized:
            quoted.append('"' + normalized + '"')
    return ' '.join(quoted)



def search_args(cfg, query: str) -> list[str]:
    identifier = query_identifier(query)
    bounded = query[:512]
    natural = ' '.join(bounded.replace('\ufeff', ' ').split())
    semantic_negation = bool(_SEMANTIC_NEGATION.search(natural))
    mode = 'lexical' if identifier or (cfg.mode != 'lexical' and semantic_negation) else cfg.mode
    text = identifier.group() if identifier else natural
    if mode == 'lexical':
        text = _quoted_lexical_query(text)
    elif not all(role in cfg.models and Path(cfg.models[role]).is_file() and
                 Path(cfg.models[role]).is_absolute() for role in
                 (['embed', 'rerank'] if cfg.rerank else ['embed'])):
        raise ValueError('semantic lookup requires approved local model paths')
    if not text.strip():
        return []
    if mode != 'lexical':
        text = 'vec: ' + text
        if mode == 'hybrid':
            text = 'lex: ' + _quoted_lexical_query(natural) + '\n' + text
    args = ['search' if mode == 'lexical' else 'query', text,
            '--format', 'json', '-n', str(cfg.top_k), '--min-score', '0']
    if mode != 'lexical':
        args += ['--explain']
        if not cfg.rerank:
            args += ['--no-rerank']
    for name in cfg.collections:
        args += ['-c', name]
    return args


def child_env(cfg) -> dict:
    # An index.yml under this explicit config directory is generated only by refresh.
    env = {k: v for k, v in os.environ.items() if k.upper() in {
        'PATH', 'PATHEXT', 'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'USERPROFILE',
        'LOCALAPPDATA', 'APPDATA', 'TEMP', 'TMP', 'HOME', 'PROGRAMFILES'}}
    env.update({'INDEX_PATH': str(cfg.state / 'index.sqlite'),
                'QMD_CONFIG_DIR': str(cfg.state / 'config'),
                'XDG_CACHE_HOME': str(cfg.state / 'cache'), 'NO_COLOR': '1',
                'QMD_FORCE_CPU': '1'})
    for role, path in cfg.models.items():
        env['QMD_' + {'embed': 'EMBED', 'generate': 'GENERATE', 'rerank': 'RERANK'}[role] + '_MODEL'] = path
    return env


def run(cfg, args: list[str], *, timeout: float | None = None) -> str:
    ensure_state(cfg)
    limit = cfg.timeout if timeout is None else timeout
    with tempfile.TemporaryDirectory(prefix='kb-process-', dir=cfg.state) as temp:
        outpath, errpath = Path(temp) / 'out', Path(temp) / 'err'
        with outpath.open('wb') as out, errpath.open('wb') as err:
            scope = ProcessScope()
            try:
                process = scope.start(cfg.command + ['--index', 'index'] + args, cwd=cfg.state,
                                      env=child_env(cfg), stdin=subprocess.DEVNULL,
                                      stdout=out, stderr=err, shell=False)
                deadline = time.monotonic() + limit
                while process.poll() is None:
                    if time.monotonic() >= deadline:
                        raise TimeoutError('QMD timeout')
                    if outpath.stat().st_size + errpath.stat().st_size > MAX_OUTPUT:
                        raise ValueError('QMD output limit exceeded')
                    time.sleep(0.02)
                if outpath.stat().st_size + errpath.stat().st_size > MAX_OUTPUT:
                    raise ValueError('QMD output limit exceeded')
                if process.returncode:
                    raise QmdExitError(process.returncode)
            finally:
                scope.close()
        return outpath.read_text(encoding='utf-8-sig')


def search(cfg, query: str) -> list:
    args = search_args(cfg, query)
    if not args:
        return []
    if not (cfg.state / 'index.sqlite').is_file() or not (cfg.state / 'config' / 'index.yml').is_file():
        raise ValueError('index not provisioned; run separate refresh')
    rows = json.loads(run(cfg, args))
    if not isinstance(rows, list) or len(rows) > 100 or not all(isinstance(r, dict) for r in rows):
        raise ValueError('invalid QMD result schema')
    if args[0] == 'query':
        for row in rows:
            explanation = row.get('explain', {})
            if not isinstance(explanation, dict):
                raise ValueError('invalid QMD score explanation')
            scores = explanation.get('vectorScores', [])
            if not isinstance(scores, list):
                raise ValueError('invalid QMD vector score schema')
            valid = [v for v in scores if isinstance(v, (float, int)) and math.isfinite(v) and -1 <= v <= 1]
            row['score'] = max(valid, default=-1)
            if cfg.mode == 'hybrid' and row['score'] < cfg.min_score and explanation.get('ftsScores'):
                row['_lexical_hit'] = True
                row['score'] = 1
    elif cfg.mode != 'lexical':
        for row in rows:
            row['_lexical_hit'] = True
            row['score'] = 1
    return rows
