#!/usr/bin/env python3
"""External command boundary for Notebook failure tests; never provisions services."""
import json
import os
from pathlib import Path
import sys

command = Path(sys.argv[0]).name
arguments = sys.argv[1:]
with open(os.environ['NOTEBOOK_COMMAND_LOG'], 'a') as log:
    log.write(json.dumps([command, *arguments]) + '\n')
if os.environ['NOTEBOOK_FAILURE_MODE'] == 'command-failure':
    print('INJECTED_COMMAND_FAILURE', file=sys.stderr)
    sys.exit(42)
if command == 'ansible':
    print('Fixture connection is available')
    sys.exit(0)
if command == 'aws':
    responses = {('ec2', 'describe-vpcs'): {'Vpcs': []},
                 ('cloudformation', 'list-stacks'): {'StackSummaries': []}}
    value = responses.get(tuple(arguments[:2]))
    if value is not None:
        print(json.dumps(value))
        sys.exit(0)
print('UNEXPECTED_EXTERNAL_COMMAND: ' + ' '.join(sys.argv), file=sys.stderr)
sys.exit(43)
