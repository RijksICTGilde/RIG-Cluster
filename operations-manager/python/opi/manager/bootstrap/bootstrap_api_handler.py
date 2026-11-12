"""Run bootstrap API actions from a declarative YAML configuration.

HTTP calls go through the HTTP connector. Variables are substituted, and an action can
capture its response for a later one.
"""

import logging
import re
from pathlib import Path
from typing import Any

from opi.connectors.http import HttpConnector
from opi.utils.yaml_util import load_yaml_safe

logger = logging.getLogger(__name__)


class BootstrapApiHandler:
    def __init__(self) -> None:
        #: Outputs of actions that carried an ``as`` key.
        self.outputs: dict[str, Any] = {}

    async def execute_config(self, yaml_path: str | Path, context: dict[str, Any]) -> None:
        """Run a YAML configuration, substituting the variables in ``context``."""
        logger.info(f"Executing bootstrap API configuration from {yaml_path}")

        config = self._load_yaml(yaml_path)
        # Context overrides the YAML's own variables.
        variables = {**config.get("variables", {}), **context}

        logger.debug(f"Context variables: {list(variables.keys())}")

        await self._process_bootstrap_actions(config.get("bootstrap"), variables)

        logger.info("Bootstrap API configuration execution completed")

    async def execute_inline_config(self, bootstrap_config: list[dict[str, Any]], context: dict[str, Any]) -> None:
        """The same, for a configuration that is already a list of actions."""
        logger.info("Executing inline bootstrap API configuration")

        await self._process_bootstrap_actions(bootstrap_config, context)

        logger.info("Inline bootstrap API configuration execution completed")

    def _load_yaml(self, yaml_path: str | Path) -> dict[str, Any]:
        path = Path(yaml_path)
        if not path.exists():
            msg = f"Bootstrap API configuration file not found: {yaml_path}"
            raise FileNotFoundError(msg)

        config = load_yaml_safe(path.read_text(encoding="utf-8"))

        if not isinstance(config, dict):
            msg = f"Invalid YAML configuration: expected dict, got {type(config)}"
            raise TypeError(msg)

        return config

    async def _process_bootstrap_actions(
        self, bootstrap_configs: list[dict[str, Any]] | None, variables: dict[str, Any]
    ) -> None:
        if not bootstrap_configs:
            logger.debug("No bootstrap actions to process")
            return

        for bootstrap_config in bootstrap_configs:
            await self._process_single_bootstrap(bootstrap_config, variables)

    async def _process_single_bootstrap(self, config: dict[str, Any], variables: dict[str, Any]) -> None:
        name = config.get("name", "unnamed")
        description = config.get("description", "")

        logger.info(f"Processing bootstrap action: {name}")
        if description:
            logger.info(f"Description: {description}")

        base_url = self._substitute_variables(config.get("base_url", ""), variables)
        if not base_url:
            logger.error(f"No base_url specified for bootstrap action '{name}', skipping")
            return

        auth_config = config.get("authentication", {"type": "none"})
        auth_config = self._substitute_variables(auth_config, variables)

        actions = config.get("actions", [])
        if not actions:
            logger.warning(f"No actions specified for bootstrap action '{name}'")
            return

        async with HttpConnector(base_url, auth_config) as http:
            for action in actions:
                await self._execute_action(action, http, variables)

    async def _execute_action(self, action: dict[str, Any], http: HttpConnector, variables: dict[str, Any]) -> None:
        action_name = action.get("name", "unnamed")
        description = action.get("description", "")

        logger.info(f"Executing action: {action_name}")
        if description:
            logger.debug(f"Description: {description}")

        method = self._substitute_variables(action.get("method", "GET"), variables)
        path = self._substitute_variables(action.get("path", "/"), variables)
        headers = self._substitute_variables(action.get("headers", {}), variables)
        body = action.get("body")

        if isinstance(body, str):
            body = self._substitute_variables(body, variables)

        expected_status = action.get("expected_status", [200])
        fail_on_error = action.get("fail_on_error", False)
        capture_as = action.get("as")

        try:
            status, response_data = await http.request(method, path, headers, body)

            if status not in expected_status:
                logger.warning(
                    f"Action '{action_name}' returned unexpected status {status}. "
                    f"Expected: {expected_status}. Response: {response_data}"
                )
                if fail_on_error:
                    msg = f"Action '{action_name}' failed with status {status}"
                    raise RuntimeError(msg)
            else:
                logger.info(f"Action '{action_name}' completed successfully with status {status}")

            if capture_as:
                self.outputs[capture_as] = response_data
                logger.debug(f"Captured response as '{capture_as}'")

        except Exception as e:
            logger.error(f"Action '{action_name}' failed with error: {e}")
            if fail_on_error:
                raise

    def _substitute_variables(self, value: Any, variables: dict[str, Any]) -> Any:  # noqa: ANN401  # a bootstrap YAML value
        """Recursively substitute {{ variable }} placeholders in a str, dict or list."""
        if isinstance(value, str):
            match = re.fullmatch(r"\{\{\s*([^}]+)\s*\}\}", value)
            if match:
                var_path = match.group(1).strip()
                return self._get_nested_value(variables, var_path)

            def replace_var(match_obj: re.Match) -> str:
                var_path = match_obj.group(1).strip()
                result = self._get_nested_value(variables, var_path)
                return str(result) if result is not None else ""

            return re.sub(r"\{\{\s*([^}]+)\s*\}\}", replace_var, value)

        if isinstance(value, dict):
            return {k: self._substitute_variables(v, variables) for k, v in value.items()}

        if isinstance(value, list):
            return [self._substitute_variables(item, variables) for item in value]

        return value

    def _get_nested_value(self, data: dict[str, Any], path: str) -> Any:  # noqa: ANN401  # a bootstrap YAML value
        """The value at a dot-separated ``path``, looked up in the outputs before the context."""
        # A filter, as in {{ var | default('value') }}.
        if "|" in path:
            var_path, filter_expr = path.split("|", 1)
            var_path = var_path.strip()
            filter_expr = filter_expr.strip()

            try:
                value = self._get_value_by_path(var_path, data)
                return value
            except KeyError:
                # ``default`` is the only filter there is.
                if filter_expr.startswith("default"):
                    match = re.match(r"default\(['\"]([^'\"]+)['\"]\)", filter_expr)
                    if match:
                        return match.group(1)
                raise

        return self._get_value_by_path(path, data)

    def _get_value_by_path(self, path: str, data: dict[str, Any]) -> Any:  # noqa: ANN401  # a bootstrap YAML value
        keys = path.split(".")

        if keys[0] in self.outputs:
            value = self.outputs
            for key in keys:
                if isinstance(value, dict) and key in value:
                    value = value[key]
                else:
                    available_keys = list(value.keys()) if isinstance(value, dict) else []
                    msg = (
                        f"Output path not found: '{path}'. Available keys in outputs: {available_keys}. "
                        f"Check that the action producing '{keys[0]}' has completed and uses 'as: {keys[0]}' in the configuration."
                    )
                    raise KeyError(msg)
            return value

        value = data
        for key in keys:
            if isinstance(value, dict) and key in value:
                value = value[key]
            else:
                available_keys = list(value.keys()) if isinstance(value, dict) else []
                msg = f"Variable path not found: '{path}'. Available context keys: {available_keys}"
                raise KeyError(msg)

        return value
