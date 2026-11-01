"""CPU-only regressions for retired boot observations and interpreter restart."""
from dataclasses import asdict
import json
from pathlib import Path
import os
import subprocess
import sys
import pytest
from media_lab_core import recovery_policy as policy


def test_retired_boot_cannot_spend_unspent_budget():
    state = policy.begin_boot(policy.State(), incident_id='incident-1', boot_id='boot-a', epoch=1)
    state = policy.begin_boot(state, incident_id='incident-1', boot_id='boot-b', epoch=2)
    original = state
    for sequence in (100, 101, 102):
        state, decision = policy.observe(state, policy.Sample(
            boot_id='boot-a', sequence=sequence, host_stalled=True,
            independent_confirmation=True, quarantine_verified=True,
            evidence_saved=True, ledger_writable=True, incident_id='incident-1', epoch=1))
        assert decision == 'reject_epoch_sample'
        assert state == original
        assert state.reboot_requests == 0


@pytest.mark.parametrize('spend', [False, True])
def test_real_interpreter_reload_preserves_epoch_and_budget(tmp_path, spend):
    state = policy.begin_boot(policy.State(), incident_id='incident-1', boot_id='boot-a', epoch=1)
    if spend:
        for i in range(3):
            state, _ = policy.observe(state, policy.Sample(
                'boot-a', i, True, True, True, True, True, 'incident-1', 1))
    state = policy.begin_boot(state, incident_id='incident-1', boot_id='boot-b', epoch=2)
    ledger = tmp_path/'ledger.json'
    ledger.write_text(json.dumps(asdict(state)))
    program = tmp_path/'reload.py'
    program.write_text('''import json, sys
from pathlib import Path
from media_lab_core.recovery_policy import State, Sample, observe
state = State(**json.loads(Path(sys.argv[1]).read_text()))
original = state
for i in range(100, 103):
    state, decision = observe(state, Sample('boot-a', i, True, True, True, True, True, 'incident-1', 1))
    assert decision == 'reject_epoch_sample'
    assert state == original
for i in range(3):
    state, decision = observe(state, Sample('boot-b', i, True, True, True, True, True, 'incident-1', 2))
assert decision == ('budget_exhausted_alert' if original.reboot_requests else 'propose_reboot')
print(decision)
''')
    env = dict(os.environ, PYTHONPATH=str(Path(policy.__file__).resolve().parents[1]),
               PYTHONDONTWRITEBYTECODE='1')
    result = subprocess.run([sys.executable, str(program), str(ledger)],
                            env=env, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ('budget_exhausted_alert' if spend else 'propose_reboot')
