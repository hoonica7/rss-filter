"""Never use real credentials or the network in these regression tests."""
import os
import socket

import pytest

for name in list(os.environ):
    if name.startswith(('GOOGLE_', 'GEMINI_', 'RSS_')):
        os.environ.pop(name)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError('Regression tests must not access the network')
    monkeypatch.setattr(socket.socket, 'connect', denied)
