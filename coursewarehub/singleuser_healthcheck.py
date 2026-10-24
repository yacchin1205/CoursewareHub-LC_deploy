#!/usr/bin/env python3
"""Check the user server API using its process-local Jupyter runtime directory."""
import json
from pathlib import Path

from jupyter_core.paths import jupyter_runtime_dir
import requests


def main():
    runtime = Path(jupyter_runtime_dir())
    candidates = list(runtime.glob('*server-*.json'))
    if not candidates:
        raise RuntimeError('The Jupyter server has not created its runtime file')
    state = json.loads(max(candidates, key=lambda p: p.stat().st_mtime).read_text())
    response = requests.get(state['url'].rstrip('/') + '/api', timeout=2,
                            proxies={'http': '', 'https': ''}, verify=False)
    response.raise_for_status()
    if not response.json().get('version'):
        raise RuntimeError('The Jupyter API did not report a version')
    print('Jupyter API healthy')


if __name__ == '__main__':
    main()
