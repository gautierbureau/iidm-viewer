"""Streamlit custom component that detects the currently-visible tab index.

The component is a 0-height iframe that reaches into the parent document,
finds the Streamlit tab bar (`[data-baseweb="tab-list"] [role="tab"]`), and
posts the index of the `aria-selected=true` tab back via setComponentValue.
It installs a capture-phase click listener so every tab click triggers a
Python rerun with the new index.

Usage::

    active = sync_active_tab(len(tabs))
    # active == 0 on first load, updates on each tab click.
"""
from __future__ import annotations

import os

import streamlit.components.v1 as components

_COMPONENT_DIR = os.path.join(
    os.path.dirname(__file__), "frontend", "active_tab", "dist"
)
_component = components.declare_component("iidm_active_tab", path=_COMPONENT_DIR)


def sync_active_tab(n_tabs: int, key: str = "active_tab_sync") -> int:
    """Return the index of the currently-visible tab (0 on first render).

    Args:
        n_tabs: Total number of tabs (passed to the component as ``nTabs``
                so the iframe can validate the index).
        key:    Streamlit widget key.
    """
    idx = _component(nTabs=n_tabs, default=0, key=key)
    try:
        return int(idx)
    except (TypeError, ValueError):
        return 0
