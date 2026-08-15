"""Manager for SMTP accounts on the platform mail relay.

There is exactly ONE piece of code that brings an account into existence
(``ensure_account``) and two callers of it: a project being processed, and the platform
setting up its own account for ZAD. That split is deliberate and load-bearing --
``plans/mailrelay.md`` (aanvulling 4) argues it at length: ZAD is not a project, it has no
project file to hang an account on, and inventing a fake project for it would create a
second kind of project that needs an exception everywhere. The price of two paths to an
account is only payable if there is one implementation behind them; two would drift, and
the platform account is the one nobody looks at.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from ruamel.yaml.scalarstring import LiteralScalarString

from opi.connectors.mail import MailAccount, MailConnector, MailRelayNotConfiguredError, create_mail_connector
from opi.core.cluster_config import get_mail_domain, get_mail_relay_host, get_mail_relay_port
from opi.core.config import settings
from opi.services import ServiceType
from opi.services.project import Project
from opi.utils.age import decrypt_password_smart_auto, encrypt_age_content, get_project_public_key
from opi.utils.naming import generate_mail_account_name
from opi.utils.passwords import generate_secure_password
from opi.utils.secrets import SendEmailSecret

if TYPE_CHECKING:
    from opi.manager.project_manager import ProjectManager

logger = logging.getLogger(__name__)

#: Config path of the send-email block on the project level.
_CONFIG_BASE = f"services/{ServiceType.SEND_EMAIL.value}/config"


class MailManager:
    """Creates, updates and removes SMTP accounts on the relay."""

    def __init__(self, project_manager: ProjectManager) -> None:
        self.project_manager = project_manager

    # --- the one account path ---------------------------------------------------

    @staticmethod
    async def ensure_account(
        connector: MailConnector,
        username: str,
        password: str,
        from_address: str,
        bounce_address: str,
        messages_per_day: int,
    ) -> MailAccount:
        """Make the relay hold exactly this account. The ONE place an account is made.

        Replay-safe by contract (``instructions/services.md``): an account that already
        exists is brought in line rather than refused, so processing a project twice is a
        no-op and a changed limit takes effect on the next run.

        Args:
            connector: The relay connector to act through.
            username: SASL username the application authenticates with.
            password: Plaintext password to set on the account.
            from_address: Address the relay pins the sender to.
            bounce_address: Address bounces come back to.
            messages_per_day: Daily budget for this account.

        Returns:
            The account as it now stands on the relay.
        """
        existing = await connector.get_principal(username)
        if existing is None:
            await connector.create_principal(
                name=username,
                password=password,
                from_address=from_address,
                messages_per_day=messages_per_day,
            )
        else:
            await connector.update_principal(
                name=username,
                password=password,
                from_address=from_address,
                messages_per_day=messages_per_day,
            )
        return MailAccount(
            username=username,
            from_address=from_address,
            bounce_address=bounce_address,
            messages_per_day=messages_per_day,
        )

    # --- caller 1: a project ----------------------------------------------------

    async def create_resources_for_deployment(self, project_data: dict[str, Any], deployment: dict[str, Any]) -> None:
        """Ensure this project's SMTP account and hand its credentials to the deployment.

        The account is per PROJECT (see ``generate_mail_account_name``), so several
        deployments of the same project share one account, one budget and one bounce
        address. The password is generated once and kept AGE-encrypted in the project
        file, which is what makes a second run reuse the account instead of resetting a
        password that running pods are still holding.
        """
        deployment_name = deployment["name"]
        cluster = deployment.get("cluster") or settings.CLUSTER_MANAGER

        if not self._deployment_uses_send_email(project_data, deployment_name):
            logger.debug(f"Deployment {deployment_name} gebruikt send-email niet, overslaan")
            return

        project_name = await self.project_manager.get_name()
        username = generate_mail_account_name(project_name)
        view = Project(project_data)
        config = view.get(_CONFIG_BASE) or {}

        from_address, bounce_address = self._addresses(cluster, username, config)
        messages_per_day = config.get("messages-per-day") or settings.MAIL_PROJECT_DEFAULT_MESSAGES_PER_DAY

        entry, password = await self._existing_account_entry(view, cluster)
        if password is None:
            password = generate_secure_password()
            entry = None

        connector = await create_mail_connector()
        account = await self.ensure_account(
            connector=connector,
            username=username,
            password=password,
            from_address=from_address,
            bounce_address=bounce_address,
            messages_per_day=messages_per_day,
        )

        if entry is None:
            await self._store_account(view, project_data, project_name, cluster, account, password)

        self.project_manager._add_secret_to_create(
            deployment_name,
            ServiceType.SEND_EMAIL.value,
            SendEmailSecret(
                host=get_mail_relay_host(cluster),
                port=get_mail_relay_port(cluster),
                username=account.username,
                password=password,
                from_address=account.from_address,
            ),
        )
        logger.info(f"Mailaccount {username} klaar voor deployment {deployment_name}")

    # --- caller 2: ZAD itself ---------------------------------------------------

    @staticmethod
    async def ensure_platform_account() -> MailAccount | None:
        """Ensure ZAD's own account on the relay, outside any project.

        Its password is NOT generated here: it comes from the SOPS secret in the relay's
        namespace and is handed to OPI as a setting, so the account can be recreated from
        the cluster's own state and there is no project file that would have to hold a
        platform credential.

        Returns ``None`` when no relay or no platform password is configured -- the
        platform account is what unblocks password reset and invite mail, and a cluster
        without a relay simply has neither yet.
        """
        if not settings.MAIL_RELAY_API_URL or not settings.MAIL_PLATFORM_PASSWORD:
            logger.info("Geen mailrelay of geen platformwachtwoord ingesteld: ZAD krijgt geen eigen mailaccount")
            return None

        cluster = settings.CLUSTER_MANAGER
        domain = get_mail_domain(cluster)
        username = settings.MAIL_PLATFORM_ACCOUNT

        password = await decrypt_password_smart_auto(settings.MAIL_PLATFORM_PASSWORD)
        connector = await create_mail_connector()
        # The same ``ensure_account`` the project path uses -- it is a staticmethod
        # precisely so this caller needs no project and no manager instance.
        account = await MailManager.ensure_account(
            connector=connector,
            username=username,
            password=password,
            from_address=f"{settings.MAIL_PLATFORM_FROM_LOCAL_PART}@{domain}",
            bounce_address=f"bounce+{username}@{domain}",
            messages_per_day=settings.MAIL_PLATFORM_MESSAGES_PER_DAY,
        )
        logger.info(f"Platform-mailaccount {username} staat klaar op de relay")
        return account

    # --- cleanup ----------------------------------------------------------------

    async def handle_service_removal(
        self,
        project_name: str,
        deployment_name: str,
        deployment_data: dict[str, Any],
        project_data: dict[str, Any],
        marked_for_deletion_service: Any = None,
    ) -> dict[str, Any]:
        """Clean up when send-email is removed from a deployment.

        The account is shared by the project's deployments, so it is only removed when NO
        deployment uses the service any more. Deleting it on the first removal would take
        the mail away from the deployments that still have the service switched on.
        """
        result = await self.delete_resources_for_deployment(project_data, deployment_data)
        result["trigger"] = "service_removal"
        return result

    async def delete_resources_for_deployment(
        self, project_data: dict[str, Any], deployment: dict[str, Any] | None
    ) -> dict[str, Any]:
        """Remove this project's SMTP account when nothing uses it any more."""
        deployment_name = (deployment or {}).get("name", "")
        results: dict[str, Any] = {
            "service": ServiceType.SEND_EMAIL.value,
            "deployment": deployment_name,
            "operations": [],
            "success": True,
            "errors": [],
        }

        if self._project_still_uses_send_email(project_data, exclude_deployment=deployment_name):
            results["operations"].append(
                {
                    "type": "send_email_cleanup",
                    "status": "skipped",
                    "reason": "Een andere deployment van dit project gebruikt het account nog",
                }
            )
            return results

        project_name = await self.project_manager.get_name()
        username = generate_mail_account_name(project_name)
        try:
            connector = await create_mail_connector()
            removed = await connector.delete_principal(username)
        except MailRelayNotConfiguredError as error:
            # No relay on this cluster: nothing was ever created, so nothing leaks.
            results["operations"].append({"type": "send_email_cleanup", "status": "skipped", "reason": str(error)})
            return results

        results["operations"].append(
            {
                "type": "send_email_cleanup",
                "status": "deleted" if removed else "already_gone",
                "account": username,
            }
        )
        return results

    # --- internals --------------------------------------------------------------

    def _addresses(self, cluster: str, username: str, config: dict[str, Any]) -> tuple[str, str]:
        """The sender and bounce address for this account.

        The bounce address is always in the PLATFORM domain, even when the project picked
        a domain of its own: SPF is checked against the envelope domain, so keeping the
        envelope on our own domain is what makes a project domain cost one DKIM record
        instead of a full DNS set (see the plan's afzenderdomein table).
        """
        platform_domain = get_mail_domain(cluster)
        local_part = config.get("from-local-part") or "noreply"
        domain = config.get("from-domain") or platform_domain
        return f"{local_part}@{domain}", f"bounce+{username}@{platform_domain}"

    async def _existing_account_entry(self, view: Project, cluster: str) -> tuple[dict[str, Any] | None, str | None]:
        """The stored account for this cluster and its decrypted password, if any."""
        accounts = view.get(f"{_CONFIG_BASE}/accounts") or []
        entry = next((item for item in accounts if item.get("cluster") == cluster), None)
        if entry is None or not entry.get("password"):
            return None, None
        return entry, await decrypt_password_smart_auto(entry["password"])

    async def _store_account(
        self,
        view: Project,
        project_data: dict[str, Any],
        project_name: str,
        cluster: str,
        account: MailAccount,
        password: str,
    ) -> None:
        """Write the account into the project file and push it immediately.

        Immediately, and for the reason the Keycloak realm block is persisted immediately:
        the generated password exists nowhere else. If a later step of the same run fails,
        the account is on the relay with a password no project file holds, and every
        re-run creates a second one.
        """
        public_key = get_project_public_key(project_data)
        if not public_key:
            raise ValueError(f"Geen project-sleutel gevonden voor {project_name}: mailaccount kan niet worden bewaard")

        accounts = view.get(f"{_CONFIG_BASE}/accounts") or []
        entry = {
            "cluster": cluster,
            "username": account.username,
            "password": LiteralScalarString(await encrypt_age_content(password, public_key)),
            "from-address": account.from_address,
            "bounce-address": account.bounce_address,
        }
        index = next((i for i, item in enumerate(accounts) if item.get("cluster") == cluster), None)
        if index is None:
            accounts.append(entry)
        else:
            accounts[index] = entry
        view.set(f"{_CONFIG_BASE}/accounts", accounts)

        await self.project_manager.save_and_commit_project(
            project_data,
            f"Persist SMTP account for {project_name} ({cluster})",
            enforce_validation=False,
        )

    def _deployment_uses_send_email(self, project_data: dict[str, Any], deployment_name: str) -> bool:
        """Whether any component of this deployment ticked send-email."""
        file_handler = self.project_manager._project_file_handler
        return file_handler.deployment_uses_service(
            project_data,
            deployment_name,
            [ServiceType.SEND_EMAIL.value],
        )

    def _project_still_uses_send_email(self, project_data: dict[str, Any], exclude_deployment: str) -> bool:
        """Whether a deployment other than ``exclude_deployment`` still uses the service."""
        for deployment in project_data.get("deployments", []) or []:
            name = deployment.get("name")
            if not name or name == exclude_deployment:
                continue
            if self._deployment_uses_send_email(project_data, name):
                return True
        return False
