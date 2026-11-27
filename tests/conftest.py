"""Global pytest configuration.

The fake-tool fixtures (``fake_tools``, ``fake_env``, ``fake_state``,
``fake_sdk``, ``fake_installer_context``) live in ``tests.fakes.fixtures`` and
are registered here as a plugin so every test package can request them.
"""

pytest_plugins = ["tests.fakes.fixtures"]
