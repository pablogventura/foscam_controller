"""Tests de utilidades del visor."""

from foscam.viewer import _stream_channel_count


class _FakeLayout:
    def __init__(self, channels):
        self.channels = channels
        self.nb_channels = len(channels)


class _FakeStream:
    def __init__(self, *, channels=None, layout=None):
        self.channels = channels
        self.layout = layout


def test_channel_count_from_nb_channels():
    s = _FakeStream(layout=_FakeLayout(("FC",)))
    assert _stream_channel_count(s) == 1


def test_channel_count_from_channels_attr():
    s = _FakeStream(channels=2)
    assert _stream_channel_count(s) == 2


def test_channel_count_default_mono():
    assert _stream_channel_count(_FakeStream()) == 1
