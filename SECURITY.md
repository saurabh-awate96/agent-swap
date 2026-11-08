# Security

aswap handles login credentials, so security reports are welcome and taken seriously.

**Please do not open a public issue for a vulnerability.** Use GitHub's private reporting: **Security → Report a vulnerability** on this repository. Include what you saw, the version (`aswap --version`), your platform, and the smallest steps to reproduce.

In scope: credential exposure (logs, argv, files with loose permissions, error messages), a way to make the web control panel (`aswap gui`) act for a page or another user, lost or overwritten logins, and anything that sends a login somewhere it should not go.

How logins are stored and what is sent over the network: [docs/safety.md](docs/safety.md). How the web dashboard is protected: [docs/gui.md](docs/gui.md).

Known and accepted: on macOS a very large credential is handed to `security add-generic-password` as a command-line argument, so it is visible to other processes of the same user for a moment.
