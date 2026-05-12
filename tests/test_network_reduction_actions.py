"""Tests for the framework-agnostic
:mod:`iidm_viewer.network_reduction_actions` helpers.

Pure validators + end-to-end reduction calls against the IEEE14
demo. Streamlit, PySide6 and NiceGUI all dispatch through this
module, so the assertions below pin the contract for every host.
"""
from __future__ import annotations

import pytest

from iidm_viewer.network_reduction_actions import (
    REDUCTION_METHODS,
    list_voltage_level_ids,
    reduce_by_ids,
    reduce_by_ids_and_depths,
    reduce_by_voltage_range,
    validate_reduce_by_ids,
    validate_reduce_by_ids_and_depths,
    validate_reduce_by_voltage_range,
)
from iidm_viewer.powsybl_worker import NetworkProxy, run


@pytest.fixture
def ieee14() -> NetworkProxy:
    """Fresh IEEE14 — every reduction test mutates in place so each
    case needs its own copy."""
    import pypowsybl.network as pn
    return NetworkProxy(run(pn.create_ieee14))


# ---------------------------------------------------------------------------
# Constants + listing
# ---------------------------------------------------------------------------
def test_reduction_methods_have_three_entries():
    assert REDUCTION_METHODS == [
        "By Voltage Range",
        "By Voltage Level IDs",
        "By Voltage Level IDs and Depths",
    ]


def test_list_voltage_level_ids_returns_sorted_ids(ieee14):
    ids = list_voltage_level_ids(ieee14)
    assert isinstance(ids, list)
    assert ids
    assert ids == sorted(ids)


# ---------------------------------------------------------------------------
# Validators
# ---------------------------------------------------------------------------
def test_validate_voltage_range_rejects_inverted_band():
    errors = validate_reduce_by_voltage_range(100.0, 50.0)
    assert any("less than" in e.lower() for e in errors)


def test_validate_voltage_range_rejects_negatives():
    errors = validate_reduce_by_voltage_range(-1.0, 100.0)
    assert any("non-negative" in e for e in errors)


def test_validate_voltage_range_accepts_valid_band():
    assert validate_reduce_by_voltage_range(0.0, 100.0) == []


def test_validate_voltage_range_rejects_non_numeric():
    errors = validate_reduce_by_voltage_range("low", 100.0)
    assert any("numeric" in e for e in errors)


def test_validate_reduce_by_ids_requires_at_least_one():
    assert validate_reduce_by_ids([])
    assert validate_reduce_by_ids([""])  # only empty strings → still empty
    assert validate_reduce_by_ids(["VL1"]) == []


def test_validate_reduce_by_ids_and_depths_requires_ids_and_non_negative_depth():
    assert validate_reduce_by_ids_and_depths([], 1)
    assert validate_reduce_by_ids_and_depths(["VL1"], -1)
    assert validate_reduce_by_ids_and_depths(["VL1"], "x")
    assert validate_reduce_by_ids_and_depths(["VL1"], 0) == []


# ---------------------------------------------------------------------------
# End-to-end reductions against IEEE14
# ---------------------------------------------------------------------------
def test_reduce_by_voltage_range_keeps_only_band():
    """Reduce to a narrow band; the voltage levels outside it should
    disappear from the network."""
    import pypowsybl.network as pn
    net = NetworkProxy(run(pn.create_ieee14))

    # IEEE14 has VLs at 13.8 / 18 / 33 / 132 kV. Keep only the
    # high-voltage portion (≥ 100 kV).
    reduce_by_voltage_range(net, v_min=100.0, v_max=200.0)
    vls = net.get_voltage_levels()
    assert not vls.empty
    nominals = sorted(vls["nominal_v"].unique().tolist())
    assert min(nominals) >= 100.0


def test_reduce_by_voltage_range_propagates_validator_errors():
    """Invalid band → ``ValueError`` from the validator (no pypowsybl
    call). Caller can surface it on a status label."""
    import pypowsybl.network as pn
    net = NetworkProxy(run(pn.create_ieee14))
    with pytest.raises(ValueError, match="less than"):
        reduce_by_voltage_range(net, v_min=200.0, v_max=100.0)


def test_reduce_by_ids_keeps_only_specified_vls():
    """Keep a single VL; every other VL should be dropped."""
    import pypowsybl.network as pn
    net = NetworkProxy(run(pn.create_ieee14))
    all_ids = list_voltage_level_ids(net)
    keep = all_ids[:1]
    reduce_by_ids(net, keep)
    remaining = list_voltage_level_ids(net)
    assert set(remaining) == set(keep)


def test_reduce_by_ids_empty_selection_raises():
    import pypowsybl.network as pn
    net = NetworkProxy(run(pn.create_ieee14))
    with pytest.raises(ValueError, match="at least one"):
        reduce_by_ids(net, [])


def test_reduce_by_ids_and_depths_expands_around_seed():
    """Seed with one VL at depth=1; the result should include the
    seed plus its direct neighbours."""
    import pypowsybl.network as pn
    net = NetworkProxy(run(pn.create_ieee14))
    seed = list_voltage_level_ids(net)[0]
    reduce_by_ids_and_depths(net, [seed], depth=1)
    remaining = list_voltage_level_ids(net)
    assert seed in remaining
    # Depth ≥ 1 should pull in at least one extra VL in IEEE14.
    assert len(remaining) >= 1


def test_reduce_by_ids_and_depths_zero_depth_keeps_only_seed():
    import pypowsybl.network as pn
    net = NetworkProxy(run(pn.create_ieee14))
    seed = list_voltage_level_ids(net)[0]
    reduce_by_ids_and_depths(net, [seed], depth=0)
    remaining = list_voltage_level_ids(net)
    assert seed in remaining


# ---------------------------------------------------------------------------
# Streamlit drift guard
# ---------------------------------------------------------------------------
def test_streamlit_network_reduction_uses_shared_actions():
    """``iidm_viewer.network_reduction`` (Streamlit) must delegate to
    the shared module — no inline calls to
    ``network.reduce_by_*`` survive."""
    pytest.importorskip("streamlit")
    import inspect
    from iidm_viewer import network_reduction

    src = inspect.getsource(network_reduction)
    assert "network_reduction_actions" in src
    # The bare pypowsybl call names must only appear inside the
    # shared module — not in the Streamlit dialog.
    assert "network.reduce_by_voltage_range" not in src
    assert "network.reduce_by_ids" not in src
    assert "network.reduce_by_ids_and_depths" not in src
