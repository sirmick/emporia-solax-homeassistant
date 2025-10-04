# Contributing to Emporia Solax Home Assistant Integration

Thank you for your interest in contributing to this project! This document provides guidelines and information for contributors.

## Development Setup

1. Fork the repository on GitHub
2. Clone your fork locally:
   ```bash
   git clone https://github.com/your-username/emporia-solax-homeassistant.git
   cd emporia-solax-homeassistant
   ```

3. Set up a virtual environment:
   ```bash
   python -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   ```

4. Install dependencies:
   ```bash
   pip install -r requirements.txt
   pip install black isort flake8  # Development tools
   ```

## Code Style

This project uses the following tools for code quality:

- **Black** for code formatting (120 character line length)
- **isort** for import sorting
- **flake8** for linting

### Running Code Quality Checks

```bash
# Format code
black poll.py

# Sort imports
isort poll.py

# Lint code
flake8 poll.py --max-line-length=120 --max-complexity=10
```

### Pre-commit Hooks (Recommended)

Install pre-commit hooks to automatically run quality checks:

```bash
pip install pre-commit
pre-commit install
```

## Testing

Currently, the project uses basic syntax checking. For any new features:

1. Test manually with your hardware setup
2. Ensure no syntax errors: `python -m py_compile poll.py`
3. Test with verbose logging: `python poll.py ... --verbose`

## Submitting Changes

1. Create a feature branch:
   ```bash
   git checkout -b feature/your-feature-name
   ```

2. Make your changes following the code style guidelines

3. Test your changes thoroughly

4. Commit your changes:
   ```bash
   git commit -m "Add: Brief description of your changes"
   ```

5. Push to your fork:
   ```bash
   git push origin feature/your-feature-name
   ```

6. Create a Pull Request on GitHub

### Commit Message Format

Use clear, descriptive commit messages. Examples:
- `Add: Support for multiple battery types`
- `Fix: Handle connection timeouts gracefully`
- `Refactor: Simplify power calculation logic`

## Reporting Issues

When reporting bugs or requesting features:

1. Check existing issues to avoid duplicates
2. Use a clear, descriptive title
3. Provide detailed steps to reproduce (for bugs)
4. Include relevant logs/output (with sensitive info redacted)
5. Specify your environment (Python version, hardware, etc.)

## Feature Requests

Feature requests are welcome! Please:

1. Check if the feature already exists or is planned
2. Describe the use case and why it's needed
3. Consider how it fits with the project's goals
4. Be open to discussion about implementation details

## Code of Conduct

This project follows a simple code of conduct:

- Be respectful and constructive in communications
- Focus on the technical merits of changes
- Help newcomers learn and contribute
- Keep discussions on-topic and professional

## License

By contributing to this project, you agree that your contributions will be licensed under the same MIT License that covers the project.