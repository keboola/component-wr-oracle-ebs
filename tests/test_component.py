import os
from unittest import mock

import pytest

from component import Component


class TestComponentInit:
    @mock.patch.dict(os.environ, {"KBC_DATADIR": "./non-existing-dir"})
    def test_run_no_cfg_fails(self) -> None:
        with pytest.raises(ValueError):
            Component()
