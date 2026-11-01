"""Bounded exact-owner stop verification. Not a cross-engine lease.

A successful stop command alone is not reclamation evidence. Unit cgroups must
be unpopulated; Docker must acknowledge removal and a successful inventory must
exclude the exact container. Unknown state raises and leaves admission closed.
Global MemAvailable/phase admission is intentionally a separate later check.
"""
from pathlib import Path
import subprocess


class ShutdownUnverified(RuntimeError):
    pass


def stop_runtime(spec, *, run=subprocess.run, cgroup_root=Path('/sys/fs/cgroup')):
    def command(args):
        try:
            reply = run(args, capture_output=True, text=True, timeout=45)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ShutdownUnverified('engine stop/inspection unavailable or timed out') from exc
        if reply.returncode != 0:
            raise ShutdownUnverified('engine stop/inspection command failed')
        return reply.stdout

    def unit_state(unit):
        raw = command(['systemctl', '--user', 'show', unit,
                       '-p', 'ActiveState', '-p', 'MainPID', '-p', 'ControlGroup'])
        values = dict(line.split('=', 1) for line in raw.splitlines() if '=' in line)
        if not {'ActiveState', 'MainPID', 'ControlGroup'} <= values.keys():
            raise ShutdownUnverified('incomplete unit state')
        return values

    if spec['kind'] == 'docker':
        name = spec['container']
        command(['docker', 'rm', '-f', name])
        names = command(['docker', 'ps', '-a', '--format', '{{.Names}}']).splitlines()
        if name in names:
            raise ShutdownUnverified('container survives shutdown')
        return {'stopped': True, 'kind': 'docker', 'owner': name}
    if spec['kind'] != 'unit':
        raise ShutdownUnverified('unsupported runtime owner kind')
    unit = spec['unit']
    if not cgroup_root.is_dir():
        raise ShutdownUnverified('cgroup hierarchy unavailable')
    before = unit_state(unit)
    if before['ActiveState'] not in ('inactive', 'failed') and not before['ControlGroup']:
        raise ShutdownUnverified('active unit has no inspectable cgroup')
    command(['systemctl', '--user', 'stop', unit])
    after = unit_state(unit)
    if after['ActiveState'] not in ('inactive', 'failed') or after['MainPID'] != '0':
        raise ShutdownUnverified('unit survives shutdown')
    for name in {before['ControlGroup'], after['ControlGroup']} - {''}:
        relative = Path(name.lstrip('/'))
        if not name.startswith('/') or '..' in relative.parts:
            raise ShutdownUnverified('invalid unit cgroup')
        group = cgroup_root / relative
        try:
            text = (group / 'cgroup.events').read_text()
        except FileNotFoundError:
            # A removed cgroup is valid; an extant group missing its events is not.
            if group.exists():
                raise ShutdownUnverified('cgroup events unavailable')
            continue
        except OSError as exc:
            raise ShutdownUnverified('cgroup unreadable') from exc
        values = dict(line.split(maxsplit=1) for line in text.splitlines() if len(line.split()) == 2)
        if values.get('populated') != '0':
            raise ShutdownUnverified('cgroup is populated or unknown')
    return {'stopped': True, 'kind': 'unit', 'owner': unit}
