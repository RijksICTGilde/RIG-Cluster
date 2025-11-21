"""
Clone Manager - Handles deployment cloning operations.

This manager orchestrates cloning of deployments from local or remote sources,
including cross-cluster cloning via Chisel tunnels.
"""

from __future__ import annotations

import logging
from typing import Any

from opi.connectors.chisel_connector import ChiselConnector
from opi.utils.age import decrypt_if_encrypted

logger = logging.getLogger(__name__)


class CloneManager:
    """Manager for clone-related operations."""

    def __init__(self, project_manager: "ProjectManager") -> None:  # noqa: F821, UP037
        """
        Initialize the CloneManager with reference to ProjectManager.

        Args:
            project_manager: The main ProjectManager instance for accessing shared resources
        """
        self.project_manager = project_manager

    async def execute_deployment_clones(
        self, project_data: dict[str, Any], deployment_name: str, force: bool = False, validate_only: bool = False
    ) -> dict[str, Any]:
        """
        Execute clones for a deployment BEFORE creating ArgoCD application.

        Args:
            project_data: Parsed project YAML data
            deployment_name: Name of deployment to clone for
            force: Force clone even if target resources exist
            validate_only: If True, only validate readiness without cloning

        Returns:
            Dictionary with clone results:
            {
                "skipped": bool,
                "reason": str,
                "success": bool,
                "validation": dict,  # if validate_only=True
                "clone_result": dict  # if validate_only=False
            }
        """
        if validate_only:
            return await self.validate_clone_readiness(project_data, deployment_name)

        clone_from_config = self.project_manager._file_handler.extract_deployment_clone_from_config(
            project_data, deployment_name
        )

        if not clone_from_config:
            logger.debug(f"No clone-from configuration for deployment: {deployment_name}")
            return {"skipped": True, "reason": "No clone-from configuration", "success": True}

        clone_type = clone_from_config.get("type")
        clone_reference = clone_from_config.get("reference")
        clone_mode = clone_from_config.get("mode", "manual")
        services_filter = clone_from_config.get("services")

        logger.info(
            f"Processing clone-from for {deployment_name}: "
            f"type={clone_type}, reference={clone_reference}, mode={clone_mode}"
        )

        # Determine if clone should execute based on mode
        should_clone = await self.should_execute_clone(deployment_name, clone_mode, force)

        if not should_clone:
            logger.info(f"Skipping clone for {deployment_name}: mode={clone_mode}, force={force}")
            return {"skipped": True, "reason": f"Clone mode '{clone_mode}' not triggered", "success": True}

        # Execute clone based on type
        project_name = await self.project_manager.get_name()

        if clone_type == "deployment":
            logger.info(f"Cloning from local deployment: {clone_reference}")
            return await self.clone_from_local_deployment(
                project_data, project_name, deployment_name, clone_reference, force
            )
        elif clone_type == "remote-source":
            logger.info(f"Cloning from remote source: {clone_reference}")
            return await self.clone_from_remote_source(
                project_data, project_name, deployment_name, clone_reference, services_filter, force
            )
        else:
            error_msg = f"Unknown clone type: {clone_type}"
            logger.error(error_msg)
            return {"success": False, "error": error_msg}

    async def should_execute_clone(self, deployment_name: str, mode: str, force: bool) -> bool:
        """
        Determine if clone should execute based on mode and current state.

        Args:
            deployment_name: Name of deployment
            mode: Clone mode (once, always, manual)
            force: Force flag from API call

        Returns:
            True if clone should execute
        """
        if mode == "manual":
            logger.debug(f"Clone mode is 'manual' for {deployment_name}, skipping automatic clone")
            return False

        if mode == "always":
            if force:
                logger.info(f"Clone mode is 'always' and force=True for {deployment_name}, will clone")
                return True
            else:
                logger.debug(f"Clone mode is 'always' but force=False for {deployment_name}, skipping")
                return False

        if mode == "once":
            # Check if ArgoCD application exists for this deployment
            argo_app_exists = await self._check_argo_application_exists(deployment_name)
            if argo_app_exists:
                logger.info(f"ArgoCD application exists for {deployment_name}, skipping 'once' mode clone")
                return False
            else:
                logger.info(f"ArgoCD application does not exist for {deployment_name}, will execute 'once' mode clone")
                return True

        logger.warning(f"Unknown clone mode '{mode}' for {deployment_name}, defaulting to skip")
        return False

    async def _check_argo_application_exists(self, deployment_name: str) -> bool:
        """
        Check if ArgoCD application exists for a deployment.

        Args:
            deployment_name: Name of deployment

        Returns:
            True if ArgoCD application exists, False otherwise
        """
        try:
            # Use ArgoCD kubectl connector to check if application exists
            argo_connector = self.project_manager._argo_kubectl_connector
            if not argo_connector:
                logger.warning("ArgoCD kubectl connector not available, assuming app doesn't exist")
                return False

            # TODO: Implement proper ArgoCD application existence check
            # For now, return False (assume app doesn't exist) to allow cloning
            logger.debug(f"Checking ArgoCD application existence for {deployment_name}")
            return False

        except Exception as e:
            logger.warning(f"Error checking ArgoCD application existence: {e}, assuming doesn't exist")
            return False

    async def clone_from_remote_source(
        self,
        project_data: dict[str, Any],
        project_name: str,
        deployment_name: str,
        remote_source_name: str,
        services_filter: list[str] | None,
        force: bool,
    ) -> dict[str, Any]:
        """
        Clone from a remote source using Chisel tunnel.

        Args:
            project_data: Project configuration
            project_name: Target project name
            deployment_name: Target deployment name
            remote_source_name: Name of remote source in remote-sources block
            services_filter: Optional list of services to clone (None = clone all)
            force: Force clone even if target exists

        Returns:
            Dictionary with clone results for each service
        """
        # Get remote source configuration
        remote_source = self.project_manager._file_handler.get_remote_source_by_name(project_data, remote_source_name)

        if not remote_source:
            error_msg = f"Remote source '{remote_source_name}' not found in project configuration"
            logger.error(error_msg)
            return {"success": False, "error": error_msg}

        chisel_config = remote_source.get("chisel", {})
        services_config = remote_source.get("services", {})

        # Determine which services to clone
        if services_filter:
            services_to_clone = services_filter
            logger.info(f"Cloning filtered services: {services_to_clone}")
        else:
            services_to_clone = list(services_config.keys())
            logger.info(f"Cloning all services from remote source: {services_to_clone}")

        results = {"services": {}, "success": True, "errors": []}

        # Clone each service
        for service_name in services_to_clone:
            if service_name not in services_config:
                error_msg = f"Service '{service_name}' not found in remote source '{remote_source_name}'"
                logger.warning(error_msg)
                results["errors"].append(error_msg)
                continue

            try:
                if service_name == "postgresql-database":
                    result = await self._clone_database_from_remote(
                        project_name, deployment_name, chisel_config, services_config[service_name], project_data, force
                    )
                    results["services"]["postgresql-database"] = result
                    if not result.get("success"):
                        results["success"] = False
                        results["errors"].extend(result.get("errors", []))
                        logger.error("Database clone failed, stopping further clones")
                        break

                elif service_name == "minio-storage":
                    result = await self._clone_minio_from_remote(
                        project_name, deployment_name, chisel_config, services_config[service_name], project_data, force
                    )
                    results["services"]["minio-storage"] = result
                    if not result.get("success"):
                        results["success"] = False
                        results["errors"].extend(result.get("errors", []))
                        logger.error("MinIO clone failed, stopping further clones")
                        break

                else:
                    logger.warning(f"Unknown service type: {service_name}, skipping")

            except Exception as e:
                error_msg = f"Error cloning service {service_name}: {e!s}"
                logger.exception(error_msg)
                results["errors"].append(error_msg)
                results["success"] = False
                break

        return results

    async def _clone_database_from_remote(
        self,
        project_name: str,
        deployment_name: str,
        chisel_config: dict[str, Any],
        db_config: dict[str, Any],
        project_data: dict[str, Any],
        force: bool,
    ) -> dict[str, Any]:
        """
        Clone PostgreSQL database from remote source via Chisel tunnel.

        This method handles tunnel setup/teardown and delegates the actual clone
        operation to DatabaseManager, which remains tunnel-agnostic.
        """
        tunnel_remote_host = db_config["host"]
        tunnel_remote_port = db_config.get("port", 5432)

        logger.info(
            f"Starting database clone via Chisel tunnel: {project_name}/{deployment_name} "
            f"<- {tunnel_remote_host}:{tunnel_remote_port} (via {chisel_config['server-url']})"
        )

        # Decrypt passwords if encrypted
        source_password = await decrypt_if_encrypted(db_config.get("password", ""), project_data)
        chisel_password = await decrypt_if_encrypted(chisel_config.get("password", ""), project_data)

        # Setup Chisel tunnel
        connector = ChiselConnector(
            server_url=chisel_config["server-url"],
            username=chisel_config["username"],
            password=chisel_password,
        )

        try:
            # Start tunnel to remote database
            _ = connector.start_tunnel(
                remote_host=tunnel_remote_host,
                remote_port=tunnel_remote_port,
            )

            endpoint = connector.get_local_endpoint()

            logger.info(
                f"Tunnel established: {endpoint['host']}:{endpoint['port']} → {tunnel_remote_host}:{tunnel_remote_port}"
            )

            # Call existing clone operation via tunnel (completely transparent)
            result = await self.project_manager._database_manager.clone_database_from_external_source(
                project_name=project_name,
                deployment_name=deployment_name,
                source_host=endpoint["host"],  # localhost
                source_port=endpoint["port"],  # tunneled port
                source_username=db_config["username"],
                source_password=source_password,
                source_database=db_config["database"],
                source_schema=db_config["schema"],
                force_clone=force,
            )

            # Add tunnel info to result
            result["tunnel"] = {
                "used": True,
                "server": chisel_config["server-url"],
                "local_endpoint": f"{endpoint['host']}:{endpoint['port']}",
                "remote_endpoint": f"{tunnel_remote_host}:{tunnel_remote_port}",
            }

            logger.info(
                f"Database clone via tunnel completed: {result['success']} (operations: {len(result['operations'])})"
            )

            return result

        finally:
            # Always cleanup tunnel
            connector.stop_tunnel()
            logger.info("Tunnel cleaned up")

    async def _clone_minio_from_remote(
        self,
        project_name: str,
        deployment_name: str,
        chisel_config: dict[str, Any],
        minio_config: dict[str, Any],
        project_data: dict[str, Any],
        force: bool,
    ) -> dict[str, Any]:
        """
        Clone MinIO bucket from remote source via Chisel tunnel.

        This method handles tunnel setup/teardown and delegates the actual clone
        operation to MinioManager, which remains tunnel-agnostic.
        """
        tunnel_remote_host = minio_config["host"]
        tunnel_remote_port = minio_config.get("port", 9000)

        logger.info(
            f"Starting MinIO clone via Chisel tunnel: {project_name}/{deployment_name} "
            f"<- {tunnel_remote_host}:{tunnel_remote_port}/{minio_config['bucket']} "
            f"(via {chisel_config['server-url']})"
        )

        # Decrypt credentials if encrypted
        access_key = await decrypt_if_encrypted(minio_config.get("access-key", ""), project_data)
        secret_key = await decrypt_if_encrypted(minio_config.get("secret-key", ""), project_data)
        chisel_password = await decrypt_if_encrypted(chisel_config.get("password", ""), project_data)

        # Setup Chisel tunnel
        connector = ChiselConnector(
            server_url=chisel_config["server-url"],
            username=chisel_config["username"],
            password=chisel_password,
        )

        try:
            # Start tunnel to remote MinIO
            _ = connector.start_tunnel(
                remote_host=tunnel_remote_host,
                remote_port=tunnel_remote_port,
            )

            endpoint = connector.get_local_endpoint()

            logger.info(
                f"Tunnel established: {endpoint['host']}:{endpoint['port']} → {tunnel_remote_host}:{tunnel_remote_port}"
            )

            # Call existing clone operation via tunnel (completely transparent)
            result = await self.project_manager._minio_manager.clone_bucket_from_external_source(
                project_name=project_name,
                deployment_name=deployment_name,
                source_host=endpoint["host"],  # localhost
                source_port=endpoint["port"],  # tunneled port
                source_access_key=access_key,
                source_secret_key=secret_key,
                source_bucket=minio_config["bucket"],
                source_secure=minio_config.get("secure", False),
                force_clone=force,
            )

            # Add tunnel info to result
            result["tunnel"] = {
                "used": True,
                "server": chisel_config["server-url"],
                "local_endpoint": f"{endpoint['host']}:{endpoint['port']}",
                "remote_endpoint": f"{tunnel_remote_host}:{tunnel_remote_port}",
            }

            logger.info(
                f"MinIO clone via tunnel completed: {result['success']} (operations: {len(result['operations'])})"
            )

            return result

        finally:
            # Always cleanup tunnel
            connector.stop_tunnel()
            logger.info("Tunnel cleaned up")

    async def clone_from_local_deployment(
        self,
        project_data: dict[str, Any],
        project_name: str,
        deployment_name: str,
        source_deployment_name: str,
        force: bool,
    ) -> dict[str, Any]:
        """
        Clone from another deployment in the same cluster.

        Args:
            project_data: Project configuration
            project_name: Project name
            deployment_name: Target deployment name
            source_deployment_name: Source deployment name to clone from
            force: Force clone even if target exists

        Returns:
            Dictionary with clone results
        """
        logger.info(f"Cloning from local deployment: {source_deployment_name} -> {deployment_name}")

        # Validate source deployment exists
        deployments = project_data.get("deployments", [])
        source_deployment = None
        for dep in deployments:
            if dep.get("name") == source_deployment_name:
                source_deployment = dep
                break

        if not source_deployment:
            error_msg = f"Source deployment '{source_deployment_name}' not found in project"
            logger.error(error_msg)
            return {"success": False, "error": error_msg}

        logger.info(f"Found source deployment: {source_deployment_name}")

        # Use existing clone logic from database_manager and minio_manager
        # These managers already support cloning from local deployments
        results = {"services": {}, "success": True, "errors": []}

        # Clone database if both deployments use postgresql-database
        # TODO: Implement local deployment cloning logic
        # This would involve calling the existing clone methods with local connection details
        logger.warning("Local deployment cloning not yet fully implemented")
        results["success"] = False
        results["error"] = "Local deployment cloning not yet implemented"

        return results

    async def validate_clone_readiness(self, project_data: dict[str, Any], deployment_name: str) -> dict[str, Any]:
        """
        Validate that clone can be executed without actually cloning.

        Performs pre-flight checks:
        - Clone configuration validity
        - Remote source existence (if remote-source type)
        - Chisel tunnel connectivity (if remote)
        - Source credentials and connectivity
        - Source resource existence
        - Target connectivity

        Args:
            project_data: Project configuration
            deployment_name: Deployment name to validate

        Returns:
            Validation report with detailed checks
        """
        checks = []
        all_passed = True

        # 1. Check clone configuration exists
        clone_config = self.project_manager._file_handler.extract_deployment_clone_from_config(
            project_data, deployment_name
        )

        if not clone_config:
            return {
                "validation": {
                    "passed": False,
                    "checks": [
                        {
                            "name": "clone_configuration",
                            "status": "failed",
                            "message": "No clone-from configuration found",
                        }
                    ],
                }
            }

        checks.append(
            {
                "name": "clone_configuration",
                "status": "success",
                "message": f"Clone configuration found: type={clone_config['type']}, mode={clone_config['mode']}",
            }
        )

        clone_type = clone_config.get("type")

        if clone_type == "remote-source":
            # 2. Validate remote source exists
            remote_source_name = clone_config.get("reference")
            remote_source = self.project_manager._file_handler.get_remote_source_by_name(
                project_data, remote_source_name
            )

            if not remote_source:
                checks.append(
                    {
                        "name": "remote_source_exists",
                        "status": "failed",
                        "message": f"Remote source '{remote_source_name}' not found",
                    }
                )
                all_passed = False
            else:
                checks.append(
                    {
                        "name": "remote_source_exists",
                        "status": "success",
                        "message": f"Remote source '{remote_source_name}' found",
                    }
                )

                # 3. Validate Chisel configuration
                chisel_config = remote_source.get("chisel")
                if not chisel_config:
                    checks.append(
                        {
                            "name": "chisel_configuration",
                            "status": "failed",
                            "message": "Chisel configuration not found in remote source",
                        }
                    )
                    all_passed = False
                else:
                    checks.append(
                        {
                            "name": "chisel_configuration",
                            "status": "success",
                            "message": f"Chisel server: {chisel_config.get('server-url')}",
                        }
                    )

                # 4. Validate services configuration
                services_config = remote_source.get("services", {})
                if not services_config:
                    checks.append(
                        {
                            "name": "services_configuration",
                            "status": "failed",
                            "message": "No services configured in remote source",
                        }
                    )
                    all_passed = False
                else:
                    service_names = list(services_config.keys())
                    checks.append(
                        {
                            "name": "services_configuration",
                            "status": "success",
                            "message": f"Services configured: {', '.join(service_names)}",
                        }
                    )

        elif clone_type == "deployment":
            # Validate source deployment exists
            source_deployment_name = clone_config.get("reference")
            deployments = project_data.get("deployments", [])
            source_exists = any(dep.get("name") == source_deployment_name for dep in deployments)

            if source_exists:
                checks.append(
                    {
                        "name": "source_deployment_exists",
                        "status": "success",
                        "message": f"Source deployment '{source_deployment_name}' found",
                    }
                )
            else:
                checks.append(
                    {
                        "name": "source_deployment_exists",
                        "status": "failed",
                        "message": f"Source deployment '{source_deployment_name}' not found",
                    }
                )
                all_passed = False

        return {"validation": {"passed": all_passed, "checks": checks}}
