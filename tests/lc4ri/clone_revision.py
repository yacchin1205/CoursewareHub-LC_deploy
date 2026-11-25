"""Select an optional GitHub revision for a construction Notebook's working copy."""
import os
import re
import subprocess

from notebook_controls import source


def resolve_revision(variable):
    selection = os.environ.get(variable)
    if not selection:
        return None
    repository, _, ref = selection.partition('@')
    if not re.fullmatch(r'[\w.-]+/[\w.-]+', repository) or any(part in ('.', '..') for part in repository.split('/')) or not ref:
        raise ValueError(f'{variable}: expected owner/repository@branch, @refs/pull/N/head or @commit')
    url = f'https://github.com/{repository}.git'
    if re.fullmatch('[0-9a-f]{40}', ref):
        return url, ref
    full_ref = ref if ref.startswith('refs/') else f'refs/heads/{ref}'
    if not (full_ref.startswith('refs/heads/') or re.fullmatch(r'refs/pull/[1-9][0-9]*/(head|merge)', full_ref)):
        raise ValueError(f'{variable}: expected a branch, pull request ref or commit')
    subprocess.run(['git', 'check-ref-format', full_ref], check=True)
    output = subprocess.check_output(['git', 'ls-remote', '--refs', '--exit-code', url, full_ref],
                                     text=True, timeout=60)
    sha, resolved_ref = output.strip().split('\t')
    if resolved_ref != full_ref or not re.fullmatch('[0-9a-f]{40}', sha):
        raise ValueError(f'{variable}: could not resolve {selection}')
    return url, sha


def pin_clone(controls, notebook, revision, repository, directory):
    clone = f'!git clone {repository} {directory}'
    index = controls.find(clone)
    text = source(notebook['cells'][index])
    if text.splitlines().count(clone) != 1:
        raise ValueError(f'Expected one complete clone command: {clone}')
    url, sha = revision
    remote = repository if repository == '{REPOS_URL}' else url
    commands = f"""!git clone {remote} {directory} && git -C {directory} fetch origin {sha} && git -C {directory} checkout --detach {sha}
assert _exit_code == 0
actual_head = !git -C {directory} rev-parse HEAD
print(actual_head)
assert actual_head == ['{sha}']"""
    controls.edit(index, text.replace(clone, commands), 'Clone and verify the selected repository revision')
