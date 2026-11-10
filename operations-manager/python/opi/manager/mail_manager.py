"""Manager for SMTP accounts on the platform mail relay.

Exactly ONE piece of code brings an account into existence (``ensure_account``), and the
callers differ only in who asks and in the budget: a project being processed, the platform
setting up ZAD's own account, and Keycloak's. Keep it one implementation: ZAD is not a
project and has no project file to hang an account on, and a second implementation would
drift, with the platform account the one nobody looks at.

Every one of them is an ordinary principal on the relay, made by the same connector through
the relay's ADMIN account, which is the one credential the infrastructure hands over.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from opi.connectors.kubectl import KubectlConnector, KubectlExecutionError
from opi.connectors.mail import MailAccount, MailConnector, MailRelayNotConfiguredError, create_mail_connector
from opi.core.cluster_config import (
    get_mail_from_address,
    get_mail_relay_host,
    get_mail_relay_port,
    get_namespace,
    get_prefixed_namespace,
)
from opi.core.config import settings
from opi.services import ServiceType
from opi.services.catalog.send_email import (
    deployment_from_address,
    deployment_from_name,
    deployment_messages_per_day,
    is_approved,
)
from opi.services.secrets import SendEmailSecret
from opi.utils.naming import (
    MAIL_PROJECT_ACCOUNT_PREFIX,
    generate_deployment_mail_account_name,
    generate_deployment_mail_sender_address,
    generate_mail_account_name,
    generate_mail_sender_address,
)
from opi.utils.passwords import generate_secure_password

if TYPE_CHECKING:
    from opi.manager.project_manager import ProjectManager

logger = logging.getLogger(__name__)


class MailAccountNameError(ValueError):
    """An account name is being used on a path that may not have it.

    The relay has one flat account namespace: ZAD's own account stands next to the project
    accounts. Two things keep a project off the platform account, and both are needed. The
    names are disjoint by construction (project accounts carry
    ``MAIL_PROJECT_ACCOUNT_PREFIX``), and the project path refuses the platform name
    outright, because that prefix only holds while nobody points ``MAIL_PLATFORM_ACCOUNT``
    INTO it, and because the project path also reads account names out of the project file,
    which a repair or an older file can carry.
    """


def _platform_accounts() -> dict[str, str]:
    """Every account the platform owns, by setting name.

    A COLLECTION and not one name, because there are two: ZAD's own (``zad-platform``) and
    Keycloak's (``zad-keycloak``). A guard that knows only one of them leaves the other open
    for a project to take over.
    """
    return {
        "MAIL_PLATFORM_ACCOUNT": settings.MAIL_PLATFORM_ACCOUNT,
        "MAIL_KEYCLOAK_ACCOUNT": settings.MAIL_KEYCLOAK_ACCOUNT,
    }


def _refuse_platform_account(username: str) -> None:
    """Refuse any platform account on the project path.

    Both directions of the collision are caught: a project account that is called like
    one of the platform accounts, and a platform account that has been configured into
    the project prefix. In either case a project would create, update or DELETE an
    account the platform sends its password-reset or login mail from.
    """
    for setting_name, platform in _platform_accounts().items():
        if username == platform:
            raise MailAccountNameError(
                f"Mailaccount {username} is een platformaccount ({setting_name}) en is niet van een project; "
                "de projectweg raakt het niet aan"
            )
        if platform.startswith(MAIL_PROJECT_ACCOUNT_PREFIX):
            raise MailAccountNameError(
                f"{setting_name} ({platform}) staat in de naamruimte van de projectaccounts "
                f"({MAIL_PROJECT_ACCOUNT_PREFIX}...): dan kan een project het platformaccount overnemen"
            )


class MailManager:
    def __init__(self, project_manager: ProjectManager) -> None:
        self.project_manager = project_manager

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
        sender_address: str = "",
        project_name: str = "",
    ) -> MailAccount:
        """Make the relay hold exactly this account. The ONE place an account is made.

        Replay-safe by contract (``docs/development/services.md``): an account that already
        exists is brought in line rather than refused, so processing a project twice is a
        no-op and a changed limit takes effect on the next run.

        Which is why the platform account is refused HERE and not only at the caller: "an
        existing account is brought in line" means a project reaching this method with ZAD's
        account name would have the relay overwrite ZAD's password and sender address.

        Args:
            connector: The relay connector to act through.
            username: SASL username the application authenticates with.
            password: Plaintext password to set on the account.
            from_address: Address the relay pins the sender to.
            bounce_address: Address bounces come back to; the same address as ``from_address``
                today.
            from_name: Display name the recipient sees, or empty for none. Empty is a valid
                outcome and not a fallback: a project simply did not choose one.
            messages_per_day: Daily budget for this account, enforced by the relay via
                ``set_rate_limit`` (RC-400). Set on all paths including platform accounts.
            is_platform_account: Only the platform caller sets this. Everything else is
                the project path and may not touch ZAD's own account.
            sender_address: Address to pin the ``From:`` header to, or empty to let the
                relay DERIVE it from the account name. Empty is the normal case, and an
                address a project chose itself would be a spoofing button (which is why
                the project path only passes one for a value a platform administrator
                approved via ``approved_from_address``, the gate in
                ``create_resources_for_deployment``, and why the connector checks the
                value again before it reaches the relay).
            project_name: The project this account sends for, for the relay's
                ``X-ZAD-Project`` header. Empty for a platform account, which sends on
                nobody's behalf and gets no header. Since RC-401 the account name no longer
                says which project it is, so this is the only way the relay can know.

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

        # The display NAME has to be told to the relay; the address it works out itself from
        # this account name (see ``_sender_address``). Written only on a difference, so
        # processing a project twice makes no settings write and no reload, while a changed
        # ``from-name`` takes effect on the next run.
        #
        # An absent name or address is a legal outcome, not a missing value: the account then
        # sends from the derived address with no name next to it.
        await connector.set_sender(username, from_name, sender_address, project_name)
        await connector.set_rate_limit(username, messages_per_day)

        return MailAccount(
            username=username,
            from_address=from_address,
            bounce_address=bounce_address,
            messages_per_day=messages_per_day,
        )

    async def create_resources_for_deployment(self, project_data: dict[str, Any], deployment: dict[str, Any]) -> None:
        """Ensure this DEPLOYMENT's SMTP account and hand its credentials to it.

        One account per deployment (see ``generate_deployment_mail_account_name``), so each
        one has its own budget, password and bounce address.

        The password comes out of the deployment's own Secret, exactly as the database
        password does, and is generated only when that Secret is CONFIRMED absent. That is
        what makes a second run reuse the account instead of resetting a password running
        pods are still holding, and it is also the repair: a relay that lost its database
        gets the stored password set again on the next run.

        Nothing happens without approval (aanvulling 6). And an approval that is WITHDRAWN
        does not just stop creating: it takes the same cleanup path as a project deletion,
        because otherwise the accounts keep standing on the relay with nobody's name on
        them.
        """
        deployment_name = deployment["name"]
        cluster = deployment.get("cluster") or settings.CLUSTER_MANAGER

        if not self._deployment_uses_send_email(project_data, deployment_name):
            logger.debug(f"Deployment {deployment_name} gebruikt send-email niet, overslaan")
            return

        project_name = await self.project_manager.get_name()

        if not is_approved(project_data):
            await self._revoke(project_data, project_name)
            return

        username = generate_deployment_mail_account_name(project_name, deployment_name)

        # This deployment's sender: the derived address carries project AND deployment (not
        # the account name, which carries the project- prefix), and the display name comes
        # from the deployment or from the project. Empty is a valid outcome: the mail then
        # leaves with the bare derived address and no name.
        derived_address = self._deployment_sender_address(cluster, project_name, deployment_name)
        # THE GATE (RC-399/RC-401). Only an address an admin approved for EXACTLY THIS VALUE
        # reaches the relay, from this deployment or from the project. In every other case an
        # empty value travels, which clears an address that stood there before, so a
        # withdrawal needs no trigger of its own. The bounce keeps coming back to the derived
        # address, so it stays traceable to this deployment.
        sender_address = deployment_from_address(project_data, deployment_name)
        from_address = sender_address or derived_address
        from_name = deployment_from_name(project_data, deployment_name)
        # Gate: the relay gets this deployment's EFFECTIVE value, never the requested one.
        messages_per_day = deployment_messages_per_day(project_data, deployment_name)

        stored = await self._read_deployment_secret(deployment_name, deployment)
        password = (stored.password if stored else "") or generate_secure_password()

        connector = await create_mail_connector()
        account = await self.ensure_account(
            connector=connector,
            username=username,
            password=password,
            from_address=from_address,
            bounce_address=derived_address,
            from_name=from_name,
            messages_per_day=messages_per_day,
            sender_address=sender_address,
            project_name=project_name,
        )

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

        await self._remove_legacy_project_account(project_data, project_name)

    @staticmethod
    async def _read_secret_or_refuse(
        kubectl: KubectlConnector, secret_name: str, namespace: str, whose: str
    ) -> dict[str, str] | None:
        """The stored Secret, or ``None`` when it is CONFIRMED absent.

        Raises ``KubectlExecutionError`` when its existence cannot be determined.
        ``get_secret`` answers ``None`` for a missing Secret AND for any kubectl failure (no
        rights, API server away, timeout), and every caller turns a ``None`` into a NEW
        password, so one unreadable moment would silently rotate an account out of its own
        password. Refusing here makes that moment a failure the next run repairs by reading
        the Secret back.
        """
        stored = await kubectl.get_secret(secret_name, namespace)
        if stored is not None:
            return stored

        if await kubectl.secret_exists(secret_name, namespace) is not False:
            raise KubectlExecutionError(
                f"Kan niet vaststellen of secret {secret_name} in {namespace} bestaat; "
                f"geen nieuw wachtwoord gegenereerd om {whose} niet te resetten"
            )
        return None

    async def _read_deployment_secret(self, deployment_name: str, deployment: dict[str, Any]) -> SendEmailSecret | None:
        """The SMTP credentials this deployment already holds, or ``None`` if it has none."""
        namespace = get_prefixed_namespace(settings.CLUSTER_MANAGER, deployment["namespace"])
        stored = await self._read_secret_or_refuse(
            self.project_manager._kubectl_connector,
            SendEmailSecret.get_secret_name(deployment_name),
            namespace,
            f"het mailaccount van {deployment_name}",
        )
        return None if stored is None else SendEmailSecret.from_k8s_secret_data(stored)

    async def _remove_legacy_project_account(self, project_data: dict[str, Any], project_name: str) -> None:
        """Remove the one-per-project account of before RC-401, once nothing uses it.

        No gap in the mail: the old account keeps standing as long as a LIVE Secret of one of
        this project's deployments still authenticates with it. Pods pick up their new account
        only once ArgoCD has synced their new Secret and restarted them, which is one round
        later than the project file being written, so deleting on the strength of the project
        file alone would take the mail away in between.

        A Secret that cannot be READ leaves the account standing, for the same reason.

        Asks the relay FIRST whether the account is even there, which is one call and the
        answer in every run after the transition. Reading every deployment's Secret, let
        alone a removal, for an account that was cleaned up months ago would be work this
        path keeps doing forever.
        """
        legacy = generate_mail_account_name(project_name)
        try:
            connector = await create_mail_connector()
        except MailRelayNotConfiguredError:
            return
        if await connector.get_principal(legacy) is None:
            return
        for deployment in project_data.get("deployments") or []:
            if not isinstance(deployment, dict) or not deployment.get("name"):
                continue
            try:
                stored = await self._read_deployment_secret(deployment["name"], deployment)
            except (KubectlExecutionError, KeyError) as error:
                logger.info(
                    f"Oud projectmailaccount {legacy} blijft staan: de gegevens van deployment "
                    f"{deployment.get('name')} zijn niet te lezen ({error})"
                )
                return
            if stored is not None and stored.username == legacy:
                logger.debug(f"Oud projectmailaccount {legacy} is nog in gebruik door {deployment['name']}")
                return
        if await self._delete_account(legacy):
            logger.info(f"Oud projectmailaccount {legacy} opgeruimd: geen enkele deployment gebruikt het nog")

    @staticmethod
    async def ensure_platform_account() -> MailAccount | None:
        """Ensure ZAD's own account on the relay, outside any project.

        An ORDINARY account: the same ``ensure_account`` a project goes through, with a
        password OPI generates itself. The password cannot come from the bootstrap, because it
        does not exist when the bootstrap runs; it is generated the first time OPI meets a
        running relay and kept in a Secret in OPI's own namespace
        (see ``_read_platform_secret``).

        Idempotent by construction. A second boot reads the stored password back and hands
        the relay the SAME one, so it makes no second account and silently replaces
        nothing. A password is generated ONLY when the Secret is absent.

        Returns ``None`` when no relay is configured; such a cluster has no password-reset
        and no invite mail either.
        """
        if not settings.MAIL_RELAY_API_URL:
            logger.info("Geen mailrelay ingesteld op dit cluster: ZAD krijgt geen eigen mailaccount")
            return None

        cluster = settings.CLUSTER_MANAGER
        username = settings.MAIL_PLATFORM_ACCOUNT
        # ZAD is not a project, so there is no project name for the plus part and no project
        # configuration to take a display name from. It sends from the BARE address, without a
        # name, which is also what the relay falls back to when it holds no sender for an
        # account. See ``_sender_address``.
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
            # Name or sender changed by configuration: the Secret must not keep answering the
            # old value to whoever reads it back.
            await MailManager._write_platform_secret(username, password, from_address)

        connector = await create_mail_connector()
        # The same ``ensure_account`` the project path uses; it is a staticmethod so this
        # caller needs no project and no manager instance.
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
        that has never met a running relay. An unreadable Secret raises rather than
        rotating ZAD out of its own account; see ``_read_secret_or_refuse``.
        """
        return await MailManager._read_secret_or_refuse(
            KubectlConnector(),
            settings.MAIL_PLATFORM_SECRET_NAME,
            get_namespace(settings.CLUSTER_MANAGER),
            "het platform-mailaccount",
        )

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

    @staticmethod
    async def ensure_keycloak_account() -> MailAccount | None:
        """Ensure KEYCLOAK's account on the relay, from the password the bootstrap made.

        A THIRD caller of ``ensure_account`` and not a variant of the second, because the two
        differ in where the password comes from. ``zad-platform`` is used by nobody but OPI,
        so OPI generates and keeps that password. Keycloak reads its password out of a file
        the bootstrap put there, so here OPI generates NOTHING and writes NOTHING back: the
        Secret comes out of git and OPI is the reconciler, not the source.

        An absent Secret is a cluster without Keycloak mail, not a failure: it is logged and
        the boot carries on, like a cluster with no relay configured at all.

        Idempotent: a second boot reads the same value out of the same Secret and hands the
        relay the same password, so nothing changes. A CHANGED value in the Secret reaches
        the relay on the next boot, which is what makes a rotation land at all.

        Returns ``None`` when there is no relay or no Secret on this cluster.
        """
        if not settings.MAIL_RELAY_API_URL:
            logger.info("Geen mailrelay ingesteld op dit cluster: Keycloak krijgt geen mailaccount")
            return None

        password = await MailManager._read_keycloak_secret()
        if not password:
            logger.info(
                f"Secret {settings.MAIL_KEYCLOAK_SECRET_NAME} ontbreekt of is leeg: "
                "dit cluster heeft geen Keycloak-mail. Genereer de clustergeheimen om hem aan te zetten."
            )
            return None

        username = settings.MAIL_KEYCLOAK_ACCOUNT
        # The BARE cluster address, with no local part and no plus part of its own: the base
        # address wanted everywhere, and also what the YAML handler writes into every realm's
        # ``smtpServer.from``. A plus part is impossible here anyway: ``zad-keycloak`` sends
        # for every realm at once, so there is no project to name. What tells login mail from
        # portal mail is the DISPLAY NAME below.
        from_address = get_mail_from_address(settings.CLUSTER_MANAGER)

        connector = await create_mail_connector()
        account = await MailManager.ensure_account(
            connector=connector,
            username=username,
            password=password,
            from_address=from_address,
            bounce_address=from_address,
            from_name=settings.MAIL_KEYCLOAK_FROM_NAME,
            messages_per_day=settings.MAIL_KEYCLOAK_MESSAGES_PER_DAY,
            is_platform_account=True,
            sender_address=from_address,
        )
        logger.info(f"Keycloak-mailaccount {username} staat klaar op de relay ({from_address})")
        return account

    @staticmethod
    async def _read_keycloak_secret() -> str | None:
        """Keycloak's mail password out of the bootstrap Secret in OPI's own namespace.

        ``None`` when the Secret is not there, which is the normal state of a cluster whose
        infrastructure secrets predate this feature.

        Unlike the platform account there is nothing to protect against a MISREAD here: a
        ``None`` leads to "no Keycloak mail on this cluster" and never to a generated password
        that overwrites anything, so an unreadable moment costs one reconcile.
        """
        namespace = get_namespace(settings.CLUSTER_MANAGER)
        stored = await KubectlConnector().get_secret(settings.MAIL_KEYCLOAK_SECRET_NAME, namespace)
        if stored is None:
            return None
        return stored.get(settings.MAIL_KEYCLOAK_SECRET_KEY) or None

    async def handle_service_removal(
        self,
        project_name: str,
        deployment_name: str,
        deployment_data: dict[str, Any],
        project_data: dict[str, Any],
        marked_for_deletion_service: Any = None,
    ) -> dict[str, Any]:
        """Clean up when send-email is removed from a deployment.

        The account belongs to the deployment, so there is nothing to weigh: its account goes
        and the other deployments keep theirs.
        """
        result = await self.delete_resources_for_deployment(project_data, deployment_data)
        result["trigger"] = "service_removal"
        return result

    async def delete_resources_for_deployment(
        self, project_data: dict[str, Any], deployment: dict[str, Any] | None
    ) -> dict[str, Any]:
        """Remove THIS deployment's SMTP account, its sender keys and its limiter."""
        deployment_name = (deployment or {}).get("name", "")
        results: dict[str, Any] = {
            "service": ServiceType.SEND_EMAIL.value,
            "deployment": deployment_name,
            "operations": [],
            "success": True,
            "errors": [],
        }
        if not deployment_name:
            results["operations"].append(
                {"type": "send_email_cleanup", "status": "skipped", "reason": "geen deployment opgegeven"}
            )
            return results

        project_name = await self.project_manager.get_name()
        username = generate_deployment_mail_account_name(project_name, deployment_name)
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

        Every removal is a PROJECT removal, because the platform account has no lifecycle
        that ends, so the platform name is refused here with no exception for anyone. That
        also covers the name that comes out of the project FILE in ``_revoke``, the one path
        where the name is read rather than computed.

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
        # And the daily budget, for the same reason: a limiter left standing applies to the
        # next account by that name.
        await connector.delete_rate_limit(username)
        return await connector.delete_principal(username)

    async def _revoke(self, project_data: dict[str, Any], project_name: str) -> None:
        """An approval that is absent, pending, denied or withdrawn: leave nothing behind.

        Every deployment of the project, because every one of them has its own account. The
        status has no memory of what it was, so this runs on every unapproved process and is
        replay-safe: an account that is already gone is a no-op. That is what makes
        "withdrawn" need no separate trigger and no event to miss.
        """
        for deployment in project_data.get("deployments") or []:
            if not isinstance(deployment, dict) or not deployment.get("name"):
                continue
            await self._delete_account(generate_deployment_mail_account_name(project_name, deployment["name"]))
        await self._remove_legacy_project_account(project_data, project_name)
        logger.info(f"Mailaccounts van {project_name} ingetrokken: het project heeft geen goedkeuring (meer)")

    @staticmethod
    def _sender_address(cluster: str, project_name: str | None) -> str:
        """The address this account's envelope carries, and its ``From:`` unless one is approved.

        The PLATFORM path calls this (with ``None``); a project's deployment goes through
        ``_deployment_sender_address``. The project branch is what the relay still derives
        for the one-per-project account of before RC-401, which stands until it is cleaned
        up, so the pairing it pins stays real for as long as that account does.

        One address for both, unless a project's own address was approved: the ``From:`` then
        carries that address and the envelope keeps this one, so the bounce stays traceable.
        The relay's ``X-ZAD-Project`` header names the project either way.

        The label goes in the plus part, which is what makes a bounce traceable without
        leaving the domain. Staying in ``rijksoverheid.nl`` is load-bearing: it publishes
        ``p=reject`` and we sign nothing with DKIM, so alignment between envelope and
        ``From:`` is the only thing that gets a message through DMARC.

        ``project_name`` is ``None`` for the platform account of ZAD itself, which is not a
        project and has none to point at. It gets the bare address, the same one the relay
        falls back to when it holds no sender for an account.

        What is returned is not a request but a REPORT of what the relay will do: the relay
        composes this address itself, by cutting the ``project-`` prefix off the authenticated
        account name (see the identity rules in the relay's configmap). The two cannot
        disagree, because both are built from the same label function.

        It is handed to the application as ``SMTP_FROM`` so a developer can see what the
        recipient gets.
        """
        base_address = get_mail_from_address(cluster)
        if project_name is None:
            return base_address
        return generate_mail_sender_address(base_address, project_name)

    @staticmethod
    def _deployment_sender_address(cluster: str, project_name: str, deployment_name: str) -> str:
        """The same report for ONE deployment's account (RC-401).

        The plus part carries project AND deployment, so a bounce is traceable to the
        deployment that sent it instead of to the project as a whole.
        """
        return generate_deployment_mail_sender_address(get_mail_from_address(cluster), project_name, deployment_name)

    def _deployment_uses_send_email(self, project_data: dict[str, Any], deployment_name: str) -> bool:
        """Whether any component of this deployment ticked send-email."""
        file_handler = self.project_manager._project_file_handler
        return file_handler.deployment_uses_service(
            project_data,
            deployment_name,
            [ServiceType.SEND_EMAIL.value],
        )
