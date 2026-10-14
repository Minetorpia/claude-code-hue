# Contributing

Contributions are welcome. Please keep the project small, dependency-light, and
safe for local smart-home use.

1. Fork the repository and create a focused branch.
2. Install a development copy with `python -m pip install -e .`.
3. Run `python -m unittest discover -s tests -v`.
4. Add tests for behavior changes and update the README when commands change.
5. Open a pull request explaining the behavior and how it was tested.

Never commit a real Hue username, Bridge address, certificate fingerprint,
Claude Code settings file, transcript, log, or state snapshot. Tests should use
temporary directories and fake clients. Do not run integration tests against
another person's lights without their permission.

By contributing, you agree that your contribution is licensed under the MIT
License included in this repository.
