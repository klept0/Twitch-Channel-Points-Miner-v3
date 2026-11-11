# Security Policy

## Supported versions

Only the latest release receives security fixes. Update to the newest release before reporting, in case the problem is already fixed.

## Reporting a vulnerability

Please **do not** open a public issue for security problems.

Report privately through GitHub instead: open the repository's **Security** tab and choose **Report a vulnerability** ([direct link](https://github.com/klept0/Twitch-Channel-Points-Miner-v3/security/advisories/new)).

Include:

- what the problem is and what an attacker could do with it
- steps to reproduce, or a proof of concept
- the version, install type (Python, Docker or Windows app) and operating system

You should get a reply within a week. Once a fix is released, the advisory is published with credit to you, unless you prefer to stay anonymous.

## Scope

Examples of problems to report privately:

- leaks of Twitch credentials, cookies or auth tokens (in logs, files, the dashboard or notifications)
- ways to reach the analytics dashboard or config editor without its password
- ways for another local or network user to run code or change settings through the miner

Problems in Twitch itself, or in third-party notification services, should go to those vendors.

Never include real passwords, cookies or tokens in a report. Redact them from any logs you attach.
