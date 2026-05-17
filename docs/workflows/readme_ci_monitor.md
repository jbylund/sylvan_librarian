# CI Monitor Workflow

This document describes the CI Monitor workflow that automatically checks the health of continuous integration on the main branch.

## Overview

The CI Monitor workflow (`.github/workflows/ci-monitor.yml`) runs every 3 hours to check if CI checks are passing on the most recent commit to the main branch.
If any CI checks have failed, it automatically creates a GitHub issue assigned to @copilot with details about the failures.

## Features

- **Scheduled Monitoring**: Runs every 3 hours using GitHub Actions cron schedule
- **Manual Triggering**: Can be manually triggered via `workflow_dispatch` for testing
- **Smart Issue Creation**: Only creates new issues if no existing `ci-failure` issues are open
- **Detailed Reporting**: Provides comprehensive failure information including links to failed runs
- **Automatic Assignment**: Issues are automatically assigned to @copilot for resolution

## How It Works

1. **Check Main Branch**: Gets the latest commit SHA from the main branch
1. **Scan Workflows**: Examines recent runs of all repository workflows (except CI Monitor itself)
1. **Detect Failures**: Identifies any workflow runs that failed on the latest main commit
1. **Create Issues**: If failures are detected and no existing CI failure issue exists, creates a new issue
1. **Provide Details**: Includes failed workflow names, failure types, and links to detailed logs

## Issue Format

When CI failures are detected, the workflow creates an issue with:

- **Title**: `CI Checks Failing on Main Branch ({commit-sha})`
- **Labels**: `ci-failure`, `bug`
- **Assignee**: `copilot`
- **Body**: Detailed information about failed checks including:
  - Commit SHA where failures occurred
  - List of failed workflows with links
  - Instructions for resolution

## Example Issue Body

```markdown
## CI Failure Detected

The automated CI monitor has detected failing checks on the main branch.

**Commit:** 5f6f80f0f16d95c4f7dfea031b09f63e7d69b528
**Failed Checks:**

- **Lint** (workflow)
  - [View details](https://github.com/jbylund/arcane_tutor/actions/runs/17803973764)

**What to do:**

1. Review the failing checks above
2. Fix any linting errors by running: `python -m ruff check --fix --unsafe-fixes`
3. Fix any failing tests by running: `python -m pytest -vvv`
4. Commit and push the fixes to main branch
5. Close this issue once CI passes

This issue was automatically created by the CI monitoring workflow.
```

## Configuration

The workflow is configured in `.github/workflows/ci-monitor.yml` with:

- **Schedule**: `0 */3 * * *` (every 3 hours)
- **Permissions**: `contents: read`, `issues: write`
- **Triggers**: `schedule`, `workflow_dispatch`

## Resolution Process

When a CI failure issue is created:

1. **Review**: Check the linked workflow runs to understand the failures
1. **Fix Locally**:
   - For linting: `python -m ruff check --fix --unsafe-fixes`
   - For tests: `python -m pytest -vvv` and fix failing tests
1. **Commit**: Push fixes to the main branch
1. **Verify**: Ensure CI passes on the new commit
1. **Close**: Close the issue once CI is healthy

## Benefits

- **Proactive Monitoring**: Catches CI failures quickly without manual checking
- **Automated Triage**: Issues are automatically assigned and labeled
- **Clear Resolution**: Provides specific instructions for fixing common issues
- **Prevents Duplication**: Won't spam with multiple issues for the same failure
- **Full Automation**: No manual intervention required for monitoring

## Monitoring Workflows

The CI Monitor currently tracks these workflows:

- **Lint**: Python code style and quality checks
- **Unit Tests**: Comprehensive test suite execution
- **Any other workflows**: Automatically detects and monitors all repository workflows

## Testing

The workflow can be tested by:

1. Manually triggering via GitHub Actions UI
1. Introducing intentional CI failures on main branch
1. Verifying issue creation and content accuracy

The workflow has been designed to handle edge cases like:

- No existing issues with `ci-failure` label
- Multiple simultaneous workflow failures
- Temporary API errors when checking workflow status
