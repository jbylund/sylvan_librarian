"""Honeybadger deployment reporting utilities."""

import logging
import os
import socket

import requests

logger = logging.getLogger(__name__)

HONEYBADGER_DEPLOY_URL = "https://api.honeybadger.io/v1/deploys"


def report_deployment() -> None:
    """Report deployment to Honeybadger if API key is configured.

    Reports deployment information to Honeybadger using:
    - Git SHA from GIT_SHA environment variable
    - Git branch from GIT_BRANCH environment variable
    - Environment from ENVIRONMENT environment variable (e.g., dev, prod)
    - Hostname from HOSTNAME environment variable or socket.gethostname()

    The environment reported to Honeybadger is formatted as {deployment_env}x{hostname}
    as specified in the requirements.

    Returns:
        None
    """
    # Check if we have the Honeybadger API key
    api_key = os.getenv("HONEYBADGER_API_KEY")
    if not api_key:
        logger.info("HONEYBADGER_API_KEY not set, skipping deployment tracking")
        return

    # Get deployment metadata
    git_sha = os.getenv("GIT_SHA", "unknown")
    git_branch = os.getenv("GIT_BRANCH", "unknown")

    # Skip if we don't have valid git metadata
    if git_sha == "unknown" or git_branch == "unknown":
        logger.warning(
            "Git metadata not available (SHA: %s, branch: %s), skipping deployment tracking",
            git_sha,
            git_branch,
        )
        return

    deployment_env = os.getenv("ENVIRONMENT", "unknown")
    hostname = os.getenv("HOSTNAME", socket.gethostname())
    repository = os.getenv("REPOSITORY_URL", "https://github.com/jbylund/sylvan_librarian")

    # Format environment as {deployment_env}-{hostname}
    environment = f"{deployment_env}-{hostname}"

    # Prepare deployment data
    deployment_data = {
        "api_key": api_key,
        "deploy": {
            "environment": environment,
            "revision": git_sha,
            "repository": repository,
            "local_username": os.getenv("USER", "docker"),
        },
    }

    try:
        logger.info(
            "Reporting deployment to Honeybadger: environment=%s, revision=%s, branch=%s",
            environment,
            git_sha,
            git_branch,
        )
        response = requests.post(
            HONEYBADGER_DEPLOY_URL,
            json=deployment_data,
            timeout=10,
        )
        response.raise_for_status()
        logger.info("Successfully reported deployment to Honeybadger")
    except requests.RequestException as e:
        logger.error("Failed to report deployment to Honeybadger: %s", e)
