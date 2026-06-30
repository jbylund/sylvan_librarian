"""Tests for deployment reporting functionality."""

import os
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import requests

from api.utils.deployment_reporting import report_deployment


class TestDeploymentReporting:
    """Test suite for Honeybadger deployment reporting."""

    @pytest.mark.parametrize(
        argnames=("env_vars", "expected_log_messages"),
        argvalues=[
            (
                {},
                ["HONEYBADGER_API_KEY not set, skipping deployment tracking"],
            ),
            (
                {
                    "HONEYBADGER_API_KEY": "test_key",
                    "GIT_SHA": "unknown",
                    "GIT_BRANCH": "unknown",
                },
                ["Git metadata not available", "skipping deployment tracking"],
            ),
            (
                {
                    "HONEYBADGER_API_KEY": "test_key",
                    "GIT_BRANCH": "main",
                },
                ["Git metadata not available"],
            ),
        ],
    )
    def test_skips_reporting_under_conditions(self, env_vars: dict, expected_log_messages: list, caplog: Any) -> None:
        """Test that deployment reporting is skipped under various conditions."""
        with patch.dict(os.environ, env_vars, clear=True):
            report_deployment()
            for expected_message in expected_log_messages:
                assert expected_message in caplog.text

    @patch("api.utils.deployment_reporting.requests.post")
    @pytest.mark.parametrize(
        argnames=("environment", "hostname", "expected_environment"),
        argvalues=[
            ("dev", "test-host", "dev-test-host"),
            ("prod", "prod-server", "prod-prod-server"),
            ("stage", "staging-host", "stage-staging-host"),
        ],
    )
    def test_successful_deployment_reporting(
        self, mock_post: Any, caplog: Any, environment: str, hostname: str, expected_environment: str
    ) -> None:
        """Test successful deployment reporting to Honeybadger."""
        # Setup mock response
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_post.return_value = mock_response

        test_env = {
            "HONEYBADGER_API_KEY": "test_api_key",
            "GIT_SHA": "abc123def456",
            "GIT_BRANCH": "main",
            "ENVIRONMENT": environment,
            "HOSTNAME": hostname,
        }

        with patch.dict(os.environ, test_env, clear=True):
            report_deployment()

        # Verify the API was called
        assert mock_post.called
        call_args = mock_post.call_args

        # Check URL
        assert call_args[0][0] == "https://api.honeybadger.io/v1/deploys"

        # Check payload structure
        payload = call_args[1]["json"]
        assert payload["api_key"] == "test_api_key"
        assert payload["deploy"]["environment"] == expected_environment
        assert payload["deploy"]["revision"] == "abc123def456"
        assert payload["deploy"]["repository"] == "https://github.com/jbylund/sylvan_librarian"

        # Check log messages
        assert "Reporting deployment to Honeybadger" in caplog.text
        assert f"environment={expected_environment}" in caplog.text
        assert "revision=abc123def456" in caplog.text
        assert "Successfully reported deployment to Honeybadger" in caplog.text

    @patch("api.utils.deployment_reporting.requests.post")
    def test_failed_deployment_reporting(self, mock_post: Any, caplog: Any) -> None:
        """Test handling of failed deployment reporting."""
        # Setup mock to raise an exception
        mock_post.side_effect = requests.RequestException("Connection error")

        test_env = {
            "HONEYBADGER_API_KEY": "test_api_key",
            "GIT_SHA": "abc123def456",
            "GIT_BRANCH": "main",
            "ENVIRONMENT": "prod",
            "HOSTNAME": "prod-host",
        }

        with patch.dict(os.environ, test_env, clear=True):
            report_deployment()

        # Check error was logged
        assert "Failed to report deployment to Honeybadger" in caplog.text

    @patch("api.utils.deployment_reporting.requests.post")
    @patch("api.utils.deployment_reporting.socket.gethostname")
    def test_default_hostname_from_socket(self, mock_gethostname: Any, mock_post: Any) -> None:
        """Test that hostname defaults to socket.gethostname() when not set."""
        mock_gethostname.return_value = "socket-hostname"
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_post.return_value = mock_response

        test_env = {
            "HONEYBADGER_API_KEY": "test_api_key",
            "GIT_SHA": "abc123def456",
            "GIT_BRANCH": "main",
            "ENVIRONMENT": "stage",
            # HOSTNAME not set, should use socket.gethostname()
        }

        with patch.dict(os.environ, test_env, clear=True):
            report_deployment()

        # Verify hostname was obtained from socket
        assert mock_gethostname.called

        # Check payload used socket hostname
        payload = mock_post.call_args[1]["json"]
        assert payload["deploy"]["environment"] == "stage-socket-hostname"

    @patch("api.utils.deployment_reporting.requests.post")
    @pytest.mark.parametrize(
        argnames=("deployment_env", "hostname", "expected_env"),
        argvalues=[
            ("dev", "host1", "dev-host1"),
            ("prod", "host2", "prod-host2"),
            ("stage", "host3", "stage-host3"),
        ],
    )
    def test_environment_format(self, mock_post: Any, deployment_env: str, hostname: str, expected_env: str) -> None:
        """Test that environment is formatted as {deployment_env}-{hostname}."""
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_post.return_value = mock_response

        test_env = {
            "HONEYBADGER_API_KEY": "test_api_key",
            "GIT_SHA": "abc123def456",
            "GIT_BRANCH": "main",
            "ENVIRONMENT": deployment_env,
            "HOSTNAME": hostname,
        }

        with patch.dict(os.environ, test_env, clear=True):
            report_deployment()

        payload = mock_post.call_args[1]["json"]
        assert payload["deploy"]["environment"] == expected_env
