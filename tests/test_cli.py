"""Thin smoke tests for the `iidm-viewer` console entry point."""
from pathlib import Path
from unittest import mock

from iidm_viewer import cli


def test_cli_invokes_streamlit_on_app_py():
    with mock.patch("iidm_viewer.cli.subprocess.call", return_value=0) as called, \
         mock.patch.object(cli.sys, "argv", ["iidm-viewer"]), \
         mock.patch.object(cli.sys, "exit") as exit_mock:
        cli.main()

    args = called.call_args.args[0]
    assert args[0:2] == ["streamlit", "run"]
    assert Path(args[2]).name == "app.py"
    exit_mock.assert_called_once_with(0)


def test_cli_forwards_extra_args():
    with mock.patch("iidm_viewer.cli.subprocess.call", return_value=0) as called, \
         mock.patch.object(cli.sys, "argv", ["iidm-viewer", "--server.port=9999"]), \
         mock.patch.object(cli.sys, "exit"):
        cli.main()

    assert called.call_args.args[0][-1] == "--server.port=9999"


def test_cli_propagates_exit_code():
    with mock.patch("iidm_viewer.cli.subprocess.call", return_value=2), \
         mock.patch.object(cli.sys, "argv", ["iidm-viewer"]), \
         mock.patch.object(cli.sys, "exit") as exit_mock:
        cli.main()
    exit_mock.assert_called_once_with(2)
