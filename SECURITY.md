# Security Policy

## Supported Versions

| Version | Supported |
|---------|-----------|
| latest  | Yes       |

## Reporting a Vulnerability

If you discover a security vulnerability in SimpleHarness, please report it responsibly:

1. **Do not** open a public GitHub issue.
2. Open a [private security advisory](https://github.com/OleJBondahl/SimpleHarness/security/advisories/new) on GitHub.
3. Include a description of the vulnerability, steps to reproduce, and any relevant logs or screenshots.

## Security Tools

This repository uses the following security tooling:

- **gitleaks** — full git history secret scanning (pre-release audit)
- **detect-secrets** — pre-commit hook to prevent new secrets from being committed
- **pip-audit** — dependency vulnerability scanning against OSV/PyPA advisories
- **pip-licenses** — dependency license compliance verification
- **bandit** — static analysis for common Python security issues
