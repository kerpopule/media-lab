"""Synthetic policy only; no GPU, systemctl, watchdog or network calls."""
from dataclasses import asdict, replace
import json
import pytest
from media_lab_core.recovery_policy import State, Sample, observe, begin_boot


def initial():
    return begin_boot(State(), incident_id='incident-1', boot_id='boot-a', epoch=1)


def sample(sequence, **changes):
    return replace(Sample(boot_id='boot-a', sequence=sequence, host_stalled=True,
        incident_id='incident-1', epoch=1, independent_confirmation=True,
        quarantine_verified=True, evidence_saved=True, ledger_writable=True), **changes)


@pytest.mark.parametrize('field', ['quarantine_verified', 'evidence_saved', 'ledger_writable'])
def test_no_reboot_until_quarantine_and_evidence_are_durable(field):
    state = initial()
    for i in range(10):
        state, decision = observe(state, sample(i, **{field: False}))
        assert decision == 'quarantine_and_alert'
        assert state.quarantined
        assert state.reboot_requests == 0


def test_reboot_proposal_requires_three_independently_confirmed_samples():
    state = initial()
    decisions = []
    for i in range(3):
        state, decision = observe(state, sample(i))
        decisions.append(decision)
    assert decisions == ['observe_and_alert', 'observe_and_alert', 'propose_reboot']
    assert state.reboot_requests == 1


def test_restart_and_boot_change_do_not_replenish_reboot_budget(tmp_path):
    state = initial()
    for i in range(3):
        state, _ = observe(state, sample(i))
    ledger = tmp_path/'ledger.json'
    ledger.write_text(json.dumps(asdict(state)))
    state = State(**json.loads(ledger.read_text()))
    state = begin_boot(state, incident_id='incident-1', boot_id='boot-b', epoch=2)
    for i in range(20):
        state, decision = observe(state, sample(i, boot_id='boot-b', epoch=2))
        assert decision == 'budget_exhausted_alert'
        assert state.reboot_requests == 1
        assert state.quarantined


def test_missing_peer_confirmation_never_proposes_reboot():
    state = initial()
    for i in range(20):
        state, decision = observe(state, sample(i, independent_confirmation=False))
        assert decision != 'propose_reboot'
    assert state.reboot_requests == 0


def test_healthy_sample_breaks_failure_streak_without_clearing_quarantine():
    state, _ = observe(initial(), sample(1))
    state, _ = observe(state, sample(2, host_stalled=False))
    assert state.failure_samples == 0
    assert state.quarantined
    state, decision = observe(state, sample(3))
    assert state.failure_samples == 1
    assert decision == 'observe_and_alert'


def test_replayed_samples_cannot_advance_decision():
    state, _ = observe(initial(), sample(5))
    original = state
    for i in [5, 4, 5]:
        state, decision = observe(state, sample(i))
        assert state == original
        assert decision == 'reject_stale_sample'


def test_missing_boot_identity_is_rejected():
    state, decision = observe(initial(), sample(1, boot_id=''))
    assert decision == 'reject_epoch_sample'
    assert state.reboot_requests == 0


def test_alternating_old_boots_after_ledger_reload_preserves_unspent_budget(tmp_path):
    state = begin_boot(initial(), incident_id='incident-1', boot_id='boot-b', epoch=2)
    ledger = tmp_path/'ledger.json'
    ledger.write_text(json.dumps(asdict(state)))
    state = State(**json.loads(ledger.read_text()))
    original = state
    for i in range(10):
        for changes in ({'boot_id': 'boot-a'}, {'boot_id': 'boot-b', 'epoch': 1},
                        {'boot_id': 'boot-c', 'epoch': 3}, {'incident_id': 'other'}):
            state, decision = observe(state, sample(100+i, **changes))
            assert state == original
            assert decision == 'reject_epoch_sample'
    for i in range(3):
        state, decision = observe(state, sample(i, boot_id='boot-b', epoch=2))
    assert decision == 'propose_reboot'


@pytest.mark.parametrize('changes', [
    {'epoch': -1}, {'epoch': True}, {'epoch': '1'}, {'epoch': 2},
    {'incident_id': ''}, {'boot_id': ''}, {'sequence': True},
    {'reboot_requests': -1}, {'reboot_requests': 2}, {'failure_samples': 4},
    {'retired_boot_ids': ['boot-a']}, {'retired_boot_ids': 'oops'},
    {'quarantined': False},
])
def test_corrupt_ledger_never_proposes_or_transitions(changes):
    state = replace(initial(), **changes)
    unchanged, decision = observe(state, sample(10))
    assert unchanged == state
    assert decision == 'invalid_ledger_alert'
    with pytest.raises(ValueError):
        begin_boot(state, incident_id='incident-1', boot_id='boot-b', epoch=2)


@pytest.mark.parametrize('changes', [
    {'epoch': 0}, {'epoch': -1}, {'epoch': True}, {'epoch': '2'}, {'epoch': 3},
    {'boot_id': 'boot-a'}, {'boot_id': ''}, {'incident_id': 'other'},
])
def test_invalid_explicit_transition_rejected(changes):
    kwargs = dict(incident_id='incident-1', boot_id='boot-b', epoch=2)
    kwargs.update(changes)
    with pytest.raises(ValueError):
        begin_boot(initial(), **kwargs)


def test_retired_boot_cannot_be_reintroduced_even_by_transition():
    state = begin_boot(initial(), incident_id='incident-1', boot_id='boot-b', epoch=2)
    with pytest.raises(ValueError):
        begin_boot(state, incident_id='incident-1', boot_id='boot-a', epoch=3)


def test_unprovisioned_ledger_never_accepts_observations():
    state, decision = observe(State(), sample(1))
    assert state == State()
    assert decision == 'invalid_ledger_alert'


@pytest.mark.parametrize('changes', [{'epoch': True}, {'epoch': '1'}, {'epoch': -1}])
def test_invalid_sample_epochs_rejected(changes):
    state, decision = observe(initial(), sample(1, **changes))
    assert state == initial()
    assert decision == 'reject_epoch_sample'


@pytest.mark.parametrize('changes', [{'sequence': True}, {'sequence': -1},
                                   {'sequence': '2'}, {'ledger_writable': 1}])
def test_invalid_sample_types_rejected(changes):
    state, decision = observe(initial(), replace(sample(1), **changes))
    assert state == initial()
    assert decision == 'reject_invalid_sample'
