"""Real socket checks for the portable launcher; no application DB is created."""
import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "fde_portable_launcher", Path(__file__).resolve().parents[1] / "packaging/launcher.py"
)
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


def test_reserved_socket_cannot_be_taken_before_server_start():
    with launcher.bind_listener("127.0.0.1", 0, False) as first:
        with pytest.raises(OSError):
            launcher.bind_listener("127.0.0.1", first.getsockname()[1], False)


def test_occupied_port_selects_another_reserved_socket():
    with launcher.bind_listener("127.0.0.1", 0, False) as occupied:
        port = occupied.getsockname()[1]
        with launcher.bind_listener("127.0.0.1", port, True) as selected:
            assert selected.getsockname()[1] != port
            with pytest.raises(OSError):
                launcher.bind_listener("127.0.0.1", selected.getsockname()[1], False)


def test_requested_free_port_is_preserved():
    with launcher.bind_listener("127.0.0.1", 0, False) as unused:
        port = unused.getsockname()[1]
    with launcher.bind_listener("127.0.0.1", port, True) as selected:
        assert selected.getsockname()[1] == port
