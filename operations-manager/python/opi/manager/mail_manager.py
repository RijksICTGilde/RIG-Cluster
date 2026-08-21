"""Manager for SMTP accounts on the platform mail relay.

There is exactly ONE piece of code that brings an account into existence
(``ensure_account``) and two callers of it: a project being processed, and the platform
setting up its own account for ZAD. That split is deliberate and load-bearing --
``plans/mailrelay.md`` (aanvulling 4 en 4b) argues it at length: ZAD is not a project, it
has no project file to hang an account on, and inventing a fake project for it would create
a second kind of project that needs an exception everywhere. The price of two CALLERS is
only payable if there is one implementation behind them; two would drift, and the platform
account is the one nobody looks at.

What is deliberately NOT different between the two: the kind of account. Both are ordinary
principals on the relay, made by the same connector through the relay's ADMIN account --
the one credential the infrastructure hands over, generated in the shared secret generation
like the Keycloak, PostgreSQL and MinIO admin passwords. The two accounts differ in who
asks for them and in their budget, in nothing else.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from ruamel.yaml.scalarstring import LiteralScalarString

from opi.connectors.kubectl import KubectlConnector, KubectlExecutionError
from opi.connectors.mail import MailAccount, MailConnector, MailRelayNotConfiguredError, create_mail_connector
from opi.core.cluster_config import get_mail_from_address, get_mail_relay_host, get_mail_relay_port, get_namespace
from opi.core.config import settings
from opi.services import ServiceType
from opi.services.catalog.send_email import is_approved
from opi.services.project import Project
from opi.utils.age import (
    decrypt_password_smart,
    encrypt_age_content,
    get_decoded_project_private_key,
    get_project_public_key,
)
from opi.utils.naming import (
    MAIL_PROJECT_ACCOUNT_PREFIX,
    generate_mail_account_name,
    generate_mail_sender_address,
)
from opi.utils.passwords import generate_secure_password
from opi.utils.secrets import SendEmailSecret

if TYPE_CHECKING:
    from opi.manager.project_manager import ProjectManager

logger = logging.getLogger(__name__)

#: Config path of the send-email block on the project level.
_CONFIG_BASE = f"services/{ServiceType.SEND_EMAIL.value}/config"


class MailAccountNameError(ValueError):
    """An account name is being used on a path that may not have it.

    The relay has one flat account namespace: ZAD's own account stands next to the
    project accounts. Two things keep a project off the platform account, and both are
    needed. The names are disjoint by construction (project accounts carry
    ``MAIL_PROJECT_ACCOUNT_PREFIX``), and the project path refuses the platform name
    outright -- because the prefix only holds while nobody points
    ``MAIL_PLATFORM_ACCOUNT`` INTO that prefix, and because the project path also gets
    account names out of the project file, which a repair or an older file can carry.
    """


def _refuse_platform_account(username: str) -> None:
    """Refuse the platform account on the project path.

    Both directions of the collision are caught: a project account that is called like
    the platform account, and a platform account that has been configured into the
    project prefix. In either case a project would create, update or DELETE the account
    ZAD sends its password-reset mail from.
    """
    platform = settings.MAIL_PLATFORM_ACCOUNT
    if username == platform:
        raise MailAccountNameError(
            f"Mailaccount {username} is het platformaccount van ZAD zelf en is niet van een project; "
            "de projectweg raakt het niet aan"
        )
    if platform.startswith(MAIL_PROJECT_ACCOUNT_PREFIX):
        raise MailAccountNameError(
            f"MAIL_PLATFORM_ACCOUNT ({platform}) staat in de naamruimte van de projectaccounts "
            f"({MAIL_PROJECT_ACCOUNT_PREFIX}...): dan kan een project het platformaccount overnemen"
        )


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
        from_name: str,
        messages_per_day: int,
        is_platform_account: bool = False,
    ) -> MailAccount:
        """Make the relay hold exactly this account. The ONE place an account is made.

        Replay-safe by contract (``instructions/services.md``): an account that already
        exists is brought in line rather than refused, so processing a project twice is a
        no-op and a changed limit takes effect on the next run.

        Which is exactly why the platform account has to be refused HERE and not only at
        the caller: "an existing account is brought in line" means a project that reaches
        this method with ZAD's account name gets the relay to overwrite ZAD's password and
        sender address. Only the platform caller says so itself.

        Args:
            connector: The relay connector to act through.
            username: SASL username the application authenticates with.
            password: Plaintext password to set on the account.
            from_address: Address the relay pins the sender to.
            bounce_address: Address bounces come back to. The same address today: envelope
                and ``From:`` were split by a plus part that bought nothing.
            from_name: Display name the recipient sees, or empty for none. Empty is a
                valid outcome and not a fallback -- a project simply did not choose one.
            messages_per_day: Daily budget recorded for this account. Not handed to the
                relay: Stalwart v0.11 has no per-account limit, so the relay enforces one
                ceiling for every account from its own configuration.
            is_platform_account: Only the platform caller sets this. Everything else is
                the project path and may not touch ZAD's own account.

        Returns:
            The account as it now stands on the relay.

        Raises:
            MailAccountNameError: The project path asked for the platform account.
        """
        if not is_platform_account:
            _refuse_platform_account(username)

        existing = await connector.get_principal(username)
        if existing is None:
            await connector.create_principal(name=username, password=password)
        else:
            await connector.update_principal(name=username, password=password)

        # The display NAME is a second thing the relay has to be told; the address it works
        # out itself from this very account name (see ``_sender_address``). Written only on
        # a difference, so processing a project twice makes no settings write and no
        # reload -- and a changed ``from-name`` takes effect on the very next run, because
        # that IS a difference.
        #
        # Nothing to warn about when it is absent: no display name is a legal outcome, and a
        # project whose name has not been written yet sends from the right ADDRESS with no
        # name next to it. That is the whole failure mode.
        await connector.set_sender_name(username, from_name)

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

        Nothing happens without approval (aanvulling 6). And an approval that is WITHDRAWN
        does not just stop creating: it takes the same cleanup path as a project deletion,
        because otherwise the account keeps standing on the relay with nobody's name on it
        and nothing left in the project file pointing at it.
        """
        deployment_name = deployment["name"]
        cluster = deployment.get("cluster") or settings.CLUSTER_MANAGER

        if not self._deployment_uses_send_email(project_data, deployment_name):
            logger.debug(f"Deployment {deployment_name} gebruikt send-email niet, overslaan")
            return

        if not is_approved(project_data):
            await self._revoke(project_data, cluster)
            return

        project_name = await self.project_manager.get_name()
        username = generate_mail_account_name(project_name)
        view = Project(project_data)
        config = view.get(_CONFIG_BASE) or {}

        # De afzender van dit project: het adres draagt de PROJECTnaam (niet de accountnaam,
        # die het voorvoegsel project- draagt en op een andere lengte wordt afgekapt), en de
        # weergavenaam komt uit de projectconfiguratie. Leeg is een geldige uitkomst: dan
        # vertrekt de post met een kaal projectadres en zonder naam.
        from_address = self._sender_address(cluster, project_name)
        bounce_address = from_address
        from_name = str(config.get("from-name") or "").strip()
        messages_per_day = config.get("messages-per-day") or settings.MAIL_PROJECT_DEFAULT_MESSAGES_PER_DAY

        entry, password = await self._existing_account_entry(view, project_data, cluster)
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
            from_name=from_name,
            messages_per_day=messages_per_day,
        )

        # Also when the entry EXISTS but no longer says what the relay holds: a project that
        # changes its local part gets a new sender address on the relay, and a project file
        # that keeps showing the old one is a wrong answer to "who does this project send
        # as". Only on a real difference, so a run that changes nothing makes no commit.
        if entry is None or self._entry_is_stale(entry, account):
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

        This is an ORDINARY account: the same ``ensure_account`` a project goes through,
        with a password OPI generates itself. There is no second kind of account and no
        second credential in the relay's own configuration -- the only thing the
        infrastructure hands over is the ADMIN credential this connector authenticates
        with, exactly as with Keycloak, PostgreSQL and MinIO.

        That is also why the password cannot come from the bootstrap: it does not exist
        when the bootstrap runs. It is generated the first time OPI meets a running relay,
        and kept in a Secret in OPI's own namespace (see ``_read_platform_secret``).

        Idempotent by construction. A second boot reads the stored password back and hands
        the relay the SAME one, so it makes no second account and silently replaces
        nothing. A password is generated ONLY when the Secret is absent.

        Returns ``None`` when no relay is configured -- the platform account is what
        unblocks password reset and invite mail, and a cluster without a relay simply has
        neither yet.
        """
        if not settings.MAIL_RELAY_API_URL:
            logger.info("Geen mailrelay ingesteld op dit cluster: ZAD krijgt geen eigen mailaccount")
            return None

        cluster = settings.CLUSTER_MANAGER
        username = settings.MAIL_PLATFORM_ACCOUNT
        # ZAD is not a project, so there is no project name to put in the plus part and no
        # project configuration to take a display name from. It sends from the BARE
        # address, without a name -- which is also what the relay falls back to when it
        # holds no sender for an account, so the platform account needs no exception
        # anywhere. See ``_sender_address``.
        from_address = MailManager._sender_address(cluster, None)
        bounce_address = from_address

        stored = await MailManager._read_platform_secret()
        password = (stored or {}).get("password") or ""
        if not password:
            # The Secret goes down BEFORE the relay call, and that order is the whole
            # safety of this path. A password on the relay that no Secret holds locks ZAD
            # out of its own account until someone notices; a password in the Secret that
            # the relay does not have yet is repaired by the very next boot, which reads
            # it back and sets it.
            password = generate_secure_password()
            await MailManager._write_platform_secret(username, password, from_address)
        elif (stored or {}).get("username") != username or (stored or {}).get("from-address") != from_address:
            # Name or sender changed by configuration: the Secret must not keep answering
            # the old thing to whoever reads it back.
            await MailManager._write_platform_secret(username, password, from_address)

        connector = await create_mail_connector()
        # The same ``ensure_account`` the project path uses -- it is a staticmethod
        # precisely so this caller needs no project and no manager instance.
        account = await MailManager.ensure_account(
            connector=connector,
            username=username,
            password=password,
            from_address=from_address,
            bounce_address=bounce_address,
            from_name="",
            messages_per_day=settings.MAIL_PLATFORM_MESSAGES_PER_DAY,
            is_platform_account=True,
        )
        logger.info(f"Platform-mailaccount {username} staat klaar op de relay")
        return account

    @staticmethod
    async def _read_platform_secret() -> dict[str, str] | None:
        """The stored platform-account credentials from OPI's own namespace.

        ``None`` when the Secret is not there yet, which is the normal state of a cluster
        that has never met a running relay.

        Raises ``KubectlExecutionError`` when the Secret's existence cannot be determined.
        That is not pedantry: ``get_secret`` answers ``None`` for a missing Secret AND for
        any kubectl failure (no rights, API server away, timeout), and the caller turns a
        ``None`` into a NEW password. So one unreadable moment would silently rotate ZAD out
        of its own mail account -- the Secret overwritten, the relay reset, and nothing in
        the way of it. Refusing here makes that moment a failed non-critical startup task
        instead, which the next boot repairs by simply reading the Secret back.
        """
        namespace = get_namespace(settings.CLUSTER_MANAGER)
        kubectl = KubectlConnector()
        stored = await kubectl.get_secret(settings.MAIL_PLATFORM_SECRET_NAME, namespace)
        if stored is not None:
            return stored

        if await kubectl.secret_exists(settings.MAIL_PLATFORM_SECRET_NAME, namespace) is not False:
            raise KubectlExecutionError(
                f"Kan niet vaststellen of secret {settings.MAIL_PLATFORM_SECRET_NAME} in {namespace} bestaat; "
                "geen nieuw wachtwoord gegenereerd om het platform-mailaccount niet te resetten"
            )
        return None

    @staticmethod
    async def _write_platform_secret(username: str, password: str, from_address: str) -> None:
        """Store the platform-account credentials in OPI's own namespace.

        OPI writes and owns this Secret; nothing in the bootstrap renders it, because its
        contents do not exist until OPI has generated them. It is written with the same
        generic secret template every other OPI-written secret uses, so an ``apply``
        replaces the whole thing and a rotation leaves nothing stale behind.
        """
        namespace = get_namespace(settings.CLUSTER_MANAGER)
        await KubectlConnector().apply_manifest(
            "manifests/generic-secret.yaml.to-sops.jinja",
            {
                "name": settings.MAIL_PLATFORM_SECRET_NAME,
                "namespace": namespace,
                "secret_type": "mail",
                "secret_k8s_type": "Opaque",
                "secret_pairs": {"username": username, "password": password, "from-address": from_address},
            },
            namespace,
        )
        logger.info(f"Platform-mailaccount {username} bewaard in secret {settings.MAIL_PLATFORM_SECRET_NAME}")

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
        removed = await self._delete_account(username)
        if removed is None:
            # No relay on this cluster: nothing was ever created, so nothing leaks.
            results["operations"].append(
                {"type": "send_email_cleanup", "status": "skipped", "reason": "geen mailrelay op dit cluster"}
            )
            return results

        results["operations"].append(
            {
                "type": "send_email_cleanup",
                "status": "deleted" if removed else "already_gone",
                "account": username,
            }
        )
        return results

    async def _delete_account(self, username: str) -> bool | None:
        """Remove the account from the relay. ``None`` when there is no relay configured.

        The ONE removal, so a withdrawn approval and a deleted project take the same path.
        Two removals would differ the moment one of them learns something the other does
        not, and the withdrawal path is the one nobody exercises.

        Every removal is a PROJECT removal -- the platform account has no lifecycle that
        ends -- so the platform name is refused here without an exception for anyone. That
        also covers the name that comes out of the project FILE in ``_revoke``, which is
        the one path where the name is not computed but read.

        Raises:
            MailAccountNameError: The name is the platform account of ZAD itself.
        """
        _refuse_platform_account(username)
        try:
            connector = await create_mail_connector()
        except MailRelayNotConfiguredError:
            return None
        # The display name goes with the account. Leaving it would keep a project's name in
        # the relay's configuration after the project is gone, and hand it to whoever next
        # gets an account by that name.
        await connector.delete_sender_name(username)
        return await connector.delete_principal(username)

    async def _revoke(self, project_data: dict[str, Any], cluster: str) -> None:
        """An approval that is absent, pending, denied or withdrawn: leave nothing behind.

        The status has no memory of what it was, so this runs on every unapproved process
        and is a no-op when there is nothing recorded -- which is what makes "withdrawn"
        need no separate trigger and no event to miss.
        """
        view = Project(project_data)
        accounts = view.get(f"{_CONFIG_BASE}/accounts") or []
        entry = next((item for item in accounts if item.get("cluster") == cluster), None)
        if entry is None:
            return

        project_name = await self.project_manager.get_name()
        username = entry.get("username") or generate_mail_account_name(project_name)
        await self._delete_account(username)

        # The entry goes too: leaving it would show a project an account it does not have,
        # and the next approval would reuse a password the relay no longer knows.
        view.set(f"{_CONFIG_BASE}/accounts", [item for item in accounts if item.get("cluster") != cluster])
        await self.project_manager.save_and_commit_project(
            project_data,
            f"Remove SMTP account for {project_name} ({cluster}): geen goedkeuring",
            enforce_validation=False,
        )
        logger.info(f"Mailaccount {username} ingetrokken: het project heeft geen goedkeuring (meer)")

    # --- internals --------------------------------------------------------------

    @staticmethod
    def _sender_address(cluster: str, project_name: str | None) -> str:
        """The one address this account sends from: the ``From:`` header AND the envelope.

        One address for both, where those two used to differ by a plus part. The
        difference bought nothing -- SPF alignment looks at the DOMAIN, and that is the
        same either way -- while it cost the recipient the ability to see which project
        wrote to them.

        The project goes in the plus part, which is what makes a bounce traceable without
        leaving the domain. Staying in ``rijksoverheid.nl`` is load-bearing: it publishes
        ``p=reject`` and we sign nothing with DKIM, so alignment between envelope and
        ``From:`` is the only thing that gets a message through DMARC.

        ``project_name`` is ``None`` for the platform account of ZAD itself, which is not a
        project and has none to point at. It gets the bare address -- the same one the
        relay falls back to when it holds no sender for an account, so the two coincide by
        construction instead of by coincidence.

        What is returned is not a request but a REPORT of what the relay will do. The relay
        composes this address ITSELF, by cutting the ``project-`` prefix off the
        authenticated account name, because Stalwart v0.11.8 turned out to have no way at
        all to look a value up per account while a message is being accepted (measured; see
        the identity rules in the relay's configmap). The two cannot disagree: both are
        built from ``mail_project_label``, so the account name always carries exactly the
        label the address puts after the ``+``.

        It is handed to the application as ``SMTP_FROM`` so a developer can see what the
        recipient gets, and written into the project file so the file answers "who does this
        project send as".
        """
        base_address = get_mail_from_address(cluster)
        if project_name is None:
            return base_address
        return generate_mail_sender_address(base_address, project_name)

    async def _existing_account_entry(
        self, view: Project, project_data: dict[str, Any], cluster: str
    ) -> tuple[dict[str, Any] | None, str | None]:
        """The stored account for this cluster and its decrypted password, if any.

        Met de PROJECTsleutel, niet de platformsleutel: ``_store_account`` versleutelt het
        wachtwoord met de publieke sleutel van het project (net als het Keycloak-blok), dus
        lezen met ``decrypt_password_smart_auto`` (de platformsleutel) strandt op "no
        identity matched any of the recipients" - en dat pas bij de TWEEDE run, want de
        eerste heeft nog niets te lezen. Gemeten 20 augustus 2026 op ai1-uit.
        """
        accounts = view.get(f"{_CONFIG_BASE}/accounts") or []
        entry = next((item for item in accounts if item.get("cluster") == cluster), None)
        if entry is None or not entry.get("password"):
            return None, None
        project_private_key = await get_decoded_project_private_key(project_data)
        return entry, await decrypt_password_smart(entry["password"], project_private_key)

    @staticmethod
    def _entry_is_stale(entry: dict[str, Any], account: MailAccount) -> bool:
        """Whether the stored entry still describes the account the relay now holds.

        Only the fields the relay is the authority on. The password is deliberately not
        compared: it is the stored one BY construction (it is where the run got it from),
        and comparing an AGE ciphertext to a plaintext would call every run stale.
        """
        return (
            entry.get("username") != account.username
            or entry.get("from-address") != account.from_address
            or entry.get("bounce-address") != account.bounce_address
        )

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
