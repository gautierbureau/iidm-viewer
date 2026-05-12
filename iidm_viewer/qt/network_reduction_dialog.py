"""PySide6 "Network Reduction" dialog.

Mirrors Streamlit's modal: three irreversible reduction modes
(voltage-range, by IDs, by IDs + depths) plus a "with boundary
lines" toggle. Each mode keeps its own widget block, swapped via a
``QStackedWidget`` driven by a radio group at the top.

On Apply, the dialog dispatches to the matching worker-routed call in
:mod:`iidm_viewer.network_reduction_actions`; the validator surface
errors land on a status label rather than crashing the dialog.
"""
from __future__ import annotations

from typing import Iterable, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QCheckBox,
    QDialog,
    QDoubleSpinBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from iidm_viewer.network_reduction_actions import (
    REDUCTION_METHODS,
    reduce_by_ids,
    reduce_by_ids_and_depths,
    reduce_by_voltage_range,
)
from iidm_viewer.powsybl_worker import NetworkProxy


class NetworkReductionDialog(QDialog):
    """Modal for the three pypowsybl reduction methods.

    Caller is responsible for running ``AppState.notify_network_changed``
    after a successful close — the dialog itself only does the
    worker-routed reduction call.
    """

    def __init__(
        self,
        network: NetworkProxy,
        vl_ids: Iterable[str],
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Network Reduction")
        self.resize(640, 520)
        self._network = network
        self._applied = False  # set True on a successful Apply.

        warn = QLabel(
            "⚠ <b>Irreversible operation.</b> The network will be permanently "
            "modified. To recover the original, reload the file."
        )
        warn.setTextFormat(Qt.RichText)
        warn.setWordWrap(True)
        warn.setStyleSheet(
            "background: #fde3e3; color: #5a0000; padding: 8px; "
            "border: 1px solid #f5a5a5; border-radius: 3px;"
        )

        # Mode radios.
        self._mode_buttons: dict[str, QRadioButton] = {}
        self._mode_group = QButtonGroup(self)
        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel("Method:"))
        for i, method in enumerate(REDUCTION_METHODS):
            rb = QRadioButton(method)
            if i == 0:
                rb.setChecked(True)
            self._mode_buttons[method] = rb
            self._mode_group.addButton(rb, i)
            mode_row.addWidget(rb)
        mode_row.addStretch(1)
        self._mode_group.idClicked.connect(self._on_mode_changed)

        self._boundary_box = QCheckBox("Replace cut lines with boundary lines")
        self._boundary_box.setToolTip(
            "Lines cut at the reduction boundary are replaced by boundary lines.",
        )

        # Mode-specific bodies in a stack.
        self._stack = QStackedWidget()
        self._stack.addWidget(self._build_range_page())
        self._stack.addWidget(self._build_ids_page(vl_ids))
        self._stack.addWidget(self._build_ids_depths_page(vl_ids))

        self._status = QLabel("")
        self._status.setWordWrap(True)

        self._apply_btn = QPushButton("Apply Reduction")
        self._apply_btn.setDefault(True)
        self._apply_btn.clicked.connect(self._on_apply_clicked)
        cancel_btn = QPushButton("Close")
        cancel_btn.clicked.connect(self.reject)

        btn_row = QHBoxLayout()
        btn_row.addWidget(self._status, 1)
        btn_row.addWidget(cancel_btn)
        btn_row.addWidget(self._apply_btn)

        layout = QVBoxLayout(self)
        layout.addWidget(warn)
        layout.addLayout(mode_row)
        layout.addWidget(self._boundary_box)
        layout.addWidget(self._stack, 1)
        layout.addLayout(btn_row)

    @property
    def applied(self) -> bool:
        """True if a reduction was successfully applied before close.

        The host uses this to decide whether to fire
        ``AppState.notify_network_changed``.
        """
        return self._applied

    # ------------------------------------------------------------------
    # Page builders
    # ------------------------------------------------------------------
    def _build_range_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addWidget(QLabel(
            "Keep all elements whose nominal voltage is within the specified "
            "range (kV).",
        ))

        self._v_min = QDoubleSpinBox()
        self._v_min.setRange(0.0, 1e9)
        self._v_min.setDecimals(2)
        self._v_min.setValue(0.0)
        self._v_max = QDoubleSpinBox()
        self._v_max.setRange(0.0, 1e9)
        self._v_max.setDecimals(2)
        self._v_max.setValue(9999.0)

        row = QHBoxLayout()
        row.addWidget(QLabel("Minimum (kV)"))
        row.addWidget(self._v_min)
        row.addSpacing(12)
        row.addWidget(QLabel("Maximum (kV)"))
        row.addWidget(self._v_max)
        row.addStretch(1)
        layout.addLayout(row)
        layout.addStretch(1)
        return page

    def _build_ids_page(self, vl_ids: Iterable[str]) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addWidget(QLabel(
            "Keep only the specified voltage levels and all elements between them.",
        ))

        self._ids_list = QListWidget()
        self._ids_list.setSelectionMode(QAbstractItemView.MultiSelection)
        for vl in vl_ids:
            self._ids_list.addItem(QListWidgetItem(str(vl)))
        layout.addWidget(self._ids_list, 1)
        return page

    def _build_ids_depths_page(self, vl_ids: Iterable[str]) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addWidget(QLabel(
            "Keep the specified voltage levels and their neighbours up to the "
            "given depth (applied to every selected voltage level).",
        ))

        self._depths_list = QListWidget()
        self._depths_list.setSelectionMode(QAbstractItemView.MultiSelection)
        for vl in vl_ids:
            self._depths_list.addItem(QListWidgetItem(str(vl)))

        self._depth_spin = QSpinBox()
        self._depth_spin.setRange(0, 100)
        self._depth_spin.setValue(1)
        depth_row = QHBoxLayout()
        depth_row.addWidget(QLabel("Depth"))
        depth_row.addWidget(self._depth_spin)
        depth_row.addStretch(1)

        layout.addWidget(self._depths_list, 1)
        layout.addLayout(depth_row)
        return page

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _selected_method(self) -> str:
        for m, btn in self._mode_buttons.items():
            if btn.isChecked():
                return m
        return REDUCTION_METHODS[0]

    def _on_mode_changed(self, mode_id: int) -> None:
        self._stack.setCurrentIndex(mode_id)
        self._status.setText("")

    def _set_error(self, text: str) -> None:
        self._status.setText(text)
        self._status.setStyleSheet("color: #b30000;")

    def _selected_ids(self, list_widget: QListWidget) -> list[str]:
        return [
            list_widget.item(i).text()
            for i in range(list_widget.count())
            if list_widget.item(i).isSelected()
        ]

    def _on_apply_clicked(self) -> None:
        self._status.setText("")
        method = self._selected_method()
        with_boundary = self._boundary_box.isChecked()
        try:
            if method == "By Voltage Range":
                reduce_by_voltage_range(
                    self._network,
                    self._v_min.value(),
                    self._v_max.value(),
                    with_boundary_lines=with_boundary,
                )
            elif method == "By Voltage Level IDs":
                reduce_by_ids(
                    self._network,
                    self._selected_ids(self._ids_list),
                    with_boundary_lines=with_boundary,
                )
            else:
                reduce_by_ids_and_depths(
                    self._network,
                    self._selected_ids(self._depths_list),
                    self._depth_spin.value(),
                    with_boundary_lines=with_boundary,
                )
        except ValueError as exc:
            self._set_error(str(exc))
            return
        except Exception as exc:
            self._set_error(f"Reduction failed: {exc}")
            return
        self._applied = True
        self.accept()
