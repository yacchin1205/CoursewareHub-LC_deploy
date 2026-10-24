"""Checked commands and SSH host verification used by the AWS construction notebooks."""
import os
from pathlib import Path
import re
import shlex
import subprocess
import tempfile
import time


def console_fingerprints(text):
    result = {}
    for line in text.splitlines():
        match = re.search(r'(\d+)\s+(SHA256:[A-Za-z0-9+/=]+).*\((RSA|ECDSA|ED25519)\)', line)
        if match:
            result[match[3]] = (match[1], match[2])
    if not result:
        raise ValueError('No SHA256 SSH host-key fingerprints found in the instance console output')
    return result


class Runtime:
    def __init__(self, ipython, *, aws_endpoint_url=None, secrets=()):
        self.ipython = ipython
        self.aws_endpoint_url = aws_endpoint_url
        self.secrets = list(secrets)

    def expand(self, command):
        return self.ipython.var_expand(command, depth=1) if self.ipython is not None else command

    def redact(self, text):
        for secret in self.secrets:
            if secret:
                text = text.replace(secret, '***')
        return text

    def run(self, argv, *, display=True, timeout=1800, input=None):
        result = subprocess.run(argv, input=input, text=True,
                                stdin=subprocess.DEVNULL if input is None else None,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
        safe_output = self.redact(result.stdout)
        if display:
            print(safe_output, flush=True)
        if result.returncode:
            raise RuntimeError('Command failed (' + str(result.returncode) + '): ' +
                               self.redact(shlex.join(argv)) + '\n' + safe_output)
        return result.stdout.splitlines()

    def shell(self, command, *, display=True):
        return self.run(['/bin/bash', '-o', 'pipefail', '-c', self.expand(command)], display=display)

    def system(self, command):
        self.shell(command)

    def aws(self, command, *, display=True):
        argv = ['aws']
        if self.aws_endpoint_url:
            argv.extend(['--endpoint-url', self.aws_endpoint_url])
        argv.extend(shlex.split(self.expand(command)))
        return self.run(argv, display=display)

    def ensure_key(self, key_path):
        path = Path(key_path).expanduser()
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not path.exists():
            self.run(['ssh-keygen', '-q', '-t', 'rsa', '-b', '3072', '-C', 'ansible',
                      '-f', str(path), '-N', ''])
        public = Path(str(path) + '.pub')
        if not public.exists():
            public.write_text('\n'.join(self.run(['ssh-keygen', '-y', '-P', '', '-f', str(path)], display=False)) + '\n')
        return str(path)

    def pin_host(self, host, expected, username, identity, known_hosts_path):
        known_hosts = Path(known_hosts_path).expanduser()
        known_hosts.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory() as directory:
            candidate = Path(directory) / 'candidate'
            for attempt in range(60):
                scan = subprocess.run(['ssh-keyscan', '-T', '3', '-t', 'ed25519', host],
                    text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
                if scan.returncode == 0 and scan.stdout.strip():
                    candidate.write_text(scan.stdout)
                    actual = self.run(['ssh-keygen', '-l', '-E', 'sha256', '-f', str(candidate)], display=False)
                    fingerprints = {line.split()[1] for line in actual}
                    if expected.get('ED25519', (None, None))[1] not in fingerprints:
                        raise AssertionError('SSH host key differs from the instance console for ' + host)
                    break
                time.sleep(2)
            else:
                raise AssertionError('SSH host key did not become available: ' + host)
            known_hosts.touch(mode=0o600, exist_ok=True)
            self.run(['ssh-keygen', '-R', host, '-f', str(known_hosts)], display=False)
            with known_hosts.open('a') as file:
                file.write(candidate.read_text())
        self.run(['ssh', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
                  '-o', 'UserKnownHostsFile=' + str(known_hosts), '-i', str(Path(identity).expanduser()),
                  username + '@' + host, 'true'])


def update_inventory(path, group, hosts):
    path = Path(path).expanduser()
    content = path.read_text() if path.exists() else ''
    lines = content.splitlines(keepends=True)
    result, skipping = [], False
    for line in lines:
        if line.strip().startswith('[') and line.strip().endswith(']'):
            skipping = line.strip() == '[' + group + ']'
        if not skipping:
            result.append(line)
    path.write_text(''.join(result).rstrip() + '\n\n[' + group + ']\n' + hosts.rstrip() + '\n')
