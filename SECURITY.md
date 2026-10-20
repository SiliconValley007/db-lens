# Security policy

db-lens connects to databases and handles credentials, so security reports are welcome and taken seriously.

## Reporting a vulnerability

Please **do not open a public issue**. Use GitHub's private reporting: on the repository page choose **Security → Report a
vulnerability**. Include the db-lens version (`db-lens --version`), the database engine and driver, and the smallest steps that
reproduce the problem. You will get an acknowledgement, and a fix or an explanation, as soon as a maintainer can look at it.

## In scope

- Ways to make `db-lens query` modify data or schema despite the read-only guard and session.
- Credential exposure: passwords in output, logs, error panels, the config file or process arguments beyond what the README documents.
- Terminal or CSV injection from database content, config corruption or data loss, TLS requirements that are silently not enforced.

## Known limits (not vulnerabilities)

- The SQL check cannot see inside user-defined functions or stored procedures; protect the database with a read-only role
  (README "Safety model").
- Oracle Database and Oracle's MySQL Server are not tested on live servers (README "Verification").
- A password given inside a URI on the command line is visible in shell history; use `--ask-password`, `--password-env` or `--password-stdin`.

## Supported versions

Only the latest release receives fixes: currently **0.7.1** (`db-lens --version`).

## Desktop window

`db-lens gui` runs statements through the same SQL guard, read-only session, timeout and row limit as `db-lens query`; its error banners and
activity log pass through the same secret redaction, and exported CSV/JSON files are created `0600` with spreadsheet formulas neutralised.
