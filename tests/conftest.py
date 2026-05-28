import pytest
import tempfile
import os
import yaml

from app.process_manager import ProcessManager, ProgramConfig


@pytest.fixture
def temp_config():
    config = {
        "programs": [
            {
                "name": "test-service",
                "command": "echo hello",
                "cwd": "/tmp",
                "autostart": False,
                "environment": {"FOO": "bar"}
            }
        ]
    }
    with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
        yaml.dump(config, f)
        path = f.name
    yield path
    os.unlink(path)


@pytest.fixture
def process_manager(temp_config):
    pm = ProcessManager()
    pm.load_config(temp_config)
    return pm


@pytest.fixture
def program_config():
    return ProgramConfig(
        name="my-program",
        command="sleep 10",
        cwd="/tmp",
        autostart=False,
        environment={}
    )
