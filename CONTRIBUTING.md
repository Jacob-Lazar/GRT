# Contributing to FL-GRT

Thank you for your interest in contributing to FL-GRT! This document provides guidelines and instructions for contributing.

## Code of Conduct

By participating in this project, you agree to abide by our [Code of Conduct](CODE_OF_CONDUCT.md).

## Getting Started

1. Fork the repository
2. Clone your fork: `git clone https://github.com/YOUR_USERNAME/GRT.git`
3. Create a feature branch: `git checkout -b feat/your-feature`
4. Make your changes
5. Run tests: `python -m unittest discover -v`
6. Commit using [Conventional Commits](https://www.conventionalcommits.org/)
7. Push and open a Pull Request

## Branch Naming

Use the `category/description` format (lowercase, kebab-case):

- `feat/` — New features
- `fix/` — Bug fixes
- `refactor/` — Code restructuring
- `docs/` — Documentation updates
- `test/` — Test additions/fixes
- `chore/` — Build scripts, config updates

## Commit Messages

Follow the [Conventional Commits](https://www.conventionalcommits.org/) specification:

```
<type>(<scope>): <subject>

<body>

<footer>
```

**Types:** `feat`, `fix`, `docs`, `style`, `refactor`, `test`, `chore`

**Example:**
```
feat(gate): add circuit breaker to batch exporter

Implement circuit breaker pattern to prevent retry storms when
the collector is unavailable.

Closes #123
```

## Code Style

- **Type hints:** All functions must have full type annotations
- **Docstrings:** Use Google-style docstrings for public classes/methods
- **Line length:** 88 characters (Black formatter)
- **Imports:** Group by stdlib → third-party → local

## Testing

- Write tests for new features
- Ensure all existing tests pass before submitting
- Use `unittest` or `pytest` for test files

## Pull Request Process

1. Update documentation if needed
2. Add tests for new functionality
3. Ensure CI checks pass
4. Request review from maintainers
5. Address review feedback

## Questions?

Open an issue or reach out to the maintainers.
