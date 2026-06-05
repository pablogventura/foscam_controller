"""Fixtures para tests de audio gate."""

import numpy as np
import pytest

from foscam.audio_gate import generate_noise_floor_with_peak, generate_sine_dbfs


@pytest.fixture
def sine_minus_50():
    return generate_sine_dbfs(-50.0)


@pytest.fixture
def sine_minus_30():
    return generate_sine_dbfs(-30.0)


@pytest.fixture
def noise_with_peak():
    return generate_noise_floor_with_peak(-45.0, -25.0)
