"""
Internationalization (i18n) support for forms.

This module provides the Translator protocol and implementations for
translating form labels, descriptions, and error messages.

The design supports multiple translation backends:
- IdentityTranslator: Returns keys unchanged (default/development)
- DictTranslator: Uses a simple dictionary for translations
- BabelTranslator: Integration with Babel/gettext (future)
"""

from typing import Protocol


class Translator(Protocol):
    """
    Protocol for translation functions.

    Translators convert message keys to localized strings.
    """

    def __call__(self, key: str) -> str:
        """
        Translate a message key.

        Args:
            key: Message key (e.g., "form.name.label")

        Returns:
            Translated string
        """
        ...


class IdentityTranslator:
    """
    Identity translator that returns keys unchanged.

    Used as the default when no i18n is configured.
    """

    def __call__(self, key: str) -> str:
        """Return the key unchanged."""
        return key


class DictTranslator:
    """
    Dictionary-based translator.

    Uses a simple dictionary for translations. Useful for testing
    or simple applications without full i18n infrastructure.
    """

    def __init__(
        self,
        translations: dict[str, str] | None = None,
        fallback_to_key: bool = True,
    ) -> None:
        """
        Initialize with translation dictionary.

        Args:
            translations: Dict mapping keys to translations
            fallback_to_key: If True, return key when translation not found
        """
        self.translations = translations or {}
        self.fallback_to_key = fallback_to_key

    def __call__(self, key: str) -> str:
        """Translate using the dictionary."""
        if key in self.translations:
            return self.translations[key]
        if self.fallback_to_key:
            return key
        raise KeyError(f"Translation not found: {key}")

    def add(self, key: str, value: str) -> None:
        """Add a translation."""
        self.translations[key] = value

    def update(self, translations: dict[str, str]) -> None:
        """Add multiple translations."""
        self.translations.update(translations)


class FormTranslator:
    """
    Form-specific translator with message formatting.

    Handles translation of form-related messages including labels,
    descriptions, and validation error messages.
    """

    def __init__(
        self,
        translator: Translator | None = None,
        prefix: str = "",
    ) -> None:
        """
        Initialize the form translator.

        Args:
            translator: Base translator to use
            prefix: Optional prefix for all keys (e.g., "forms.")
        """
        self._translator = translator or IdentityTranslator()
        self._prefix = prefix

    def __call__(self, key: str) -> str:
        """Translate a key with optional prefix."""
        full_key = f"{self._prefix}{key}" if self._prefix else key
        return self._translator(full_key)

    def translate_field_label(self, key: str) -> str:
        """Translate a field label."""
        return self(key)

    def translate_field_description(self, key: str) -> str:
        """Translate a field description."""
        return self(key)

    def translate_error(self, key: str, **kwargs: str) -> str:
        """
        Translate a validation error message.

        Args:
            key: Error message key
            **kwargs: Format arguments for the message

        Returns:
            Translated and formatted error message
        """
        translated = self(key)
        if kwargs:
            try:
                return translated.format(**kwargs)
            except KeyError:
                return translated
        return translated

    def translate_button(self, key: str) -> str:
        """Translate a button label."""
        return self(key)


# Default Dutch translations for OPI forms
DEFAULT_NL_TRANSLATIONS = {
    # Common labels
    "common.submit": "Verzenden",
    "common.save": "Opslaan",
    "common.cancel": "Annuleren",
    "common.add": "Toevoegen",
    "common.remove": "Verwijderen",
    "common.edit": "Bewerken",
    "common.delete": "Verwijderen",
    "common.back": "Terug",
    "common.next": "Volgende",
    "common.previous": "Vorige",
    "common.yes": "Ja",
    "common.no": "Nee",
    "common.loading": "Laden...",
    "common.error": "Fout",
    "common.success": "Succes",
    # Validation errors
    "validation.required": "Dit veld is verplicht",
    "validation.min_length": "Minimaal {min} tekens vereist",
    "validation.max_length": "Maximaal {max} tekens toegestaan",
    "validation.email": "Voer een geldig e-mailadres in",
    "validation.url": "Voer een geldige URL in",
    "validation.number": "Voer een geldig getal in",
    "validation.integer": "Voer een geheel getal in",
    "validation.positive": "Voer een positief getal in",
    "validation.pattern": "Dit veld voldoet niet aan het vereiste formaat",
    "validation.unique": "Deze waarde bestaat al",
    # Project form labels
    "project.display_name": "Projectnaam",
    "project.display_name.description": "Een beschrijvende naam voor uw project",
    "project.description": "Projectomschrijving",
    "project.description.description": "Korte beschrijving van het doel en de scope van het project",
    "project.cluster": "Cluster",
    "project.cluster.description": "Selecteer het gewenste cluster voor uw project",
    "project.services": "Beschikbare Services",
    "project.services.description": "Selecteer de services die u wilt activeren voor uw project",
    "project.users": "Projectleden",
    "project.users.description": "Voeg teamleden toe die toegang moeten krijgen tot het project",
    # Component form labels
    "component.type": "Component Type",
    "component.type.description": "Het type component",
    "component.port": "Poort",
    "component.port.description": "Poort waarop de applicatie draait",
    "component.image": "Container Image",
    "component.image.description": "Docker image van uw applicatie",
    "component.path": "Publicatie Pad",
    "component.path.description": "Het pad waarop dit component gepubliceerd wordt",
    "component.cpu_limit": "CPU Limiet",
    "component.cpu_limit.description": "Aantal CPU cores toegewezen aan dit component",
    "component.memory_limit": "Memory Limiet",
    "component.memory_limit.description": "Maximum RAM geheugen beschikbaar voor dit component",
    "component.env_vars": "Environment Variables",
    "component.env_vars.description": "Environment variables voor dit component",
    "component.aliases": "Aliases",
    "component.aliases.description": "Aliases voor systeemvariabelen",
    # User form labels
    "user.email": "E-mailadres",
    "user.email.description": "Rijksoverheid e-mailadres",
    "user.role": "Rol",
    "user.role.description": "De rol van de gebruiker in het project",
    # Service labels
    "service.web": "Publiceren op het web",
    "service.web.description": "Maak de applicatie toegankelijk via het publieke internet",
    "service.sso": "Single Sign-On Rijk",
    "service.sso.description": "Integreer met de Rijksoverheid SSO voor veilige authenticatie",
    "service.persistent_storage": "Permanente opslag",
    "service.persistent_storage.description": "Gegevens blijven bewaard tijdens de levenscyclus",
    "service.temp_storage": "Tijdelijke schijfruimte",
    "service.temp_storage.description": "Gegevens worden niet bewaard bij herstart",
    "service.postgresql": "PostgreSQL Database",
    "service.postgresql.description": "Database service voor applicaties",
    "service.minio": "MinIO Object Storage",
    "service.minio.description": "S3-compatible object storage voor documenten en bestanden",
    # Domain configuration
    "domain.mode": "Web Adres Configuratie",
    "domain.mode.description": "Kies hoe de URLs voor uw componenten worden gegenereerd",
    "domain.subdomain": "Aangepast Subdomein",
    "domain.subdomain.description": "Specificeer een custom subdomein voor alle componenten",
    # Form titles
    "project.form.title": "Project Aanmaken",
    "project.details": "Projectgegevens",
    "project.web_address": "Web Adres Configuratie",
    "project.services.title": "Beschikbare Services",
    "project.users.title": "Projectleden",
    "project.components": "Componenten",
    "project.components.description": "De componenten die gedeployed worden",
    "project.components.title": "Componenten",
    "component.details": "Component Details",
    # Action labels
    "common.add_user": "Lid Toevoegen",
    "common.add_component": "Component Toevoegen",
    "common.add_repository": "Repository Toevoegen",
    # Project file form
    "project.edit.title": "Project Bewerken",
    "project.basic_info": "Basisgegevens",
    "project.infrastructure": "Infrastructuur",
    "project.team": "Team",
    "project.name": "Projectnaam (technisch)",
    "project.name.description": "Unieke identifier (lowercase, alleen letters, cijfers en koppeltekens)",
    "project.clusters": "Beschikbare Clusters",
    "project.clusters.description": "Selecteer de clusters waarop dit project kan worden gedeployed",
    "project.repositories": "Repositories",
    "project.repositories.description": "Git repositories met broncode",
    "project.deployments": "Deployments",
    "project.deployments.description": "Actieve deployments op clusters",
    # Component form
    "component.name": "Component Naam",
    "component.name.description": "Unieke naam binnen het project",
    "component.ports": "Poorten",
    "component.ports.description": "Netwerk poort configuratie",
    "component.ports.inbound": "Inbound Poorten",
    "component.ports.inbound.description": "Poorten waarop verkeer binnenkomt",
    "component.ports.outbound": "Outbound Poorten",
    "component.ports.outbound.description": "Poorten voor uitgaand verkeer",
    "component.resources": "Resources",
    "component.resources.description": "CPU en geheugen limieten",
    "component.services": "Gebruikte Services",
    "component.services.description": "Services die dit component gebruikt",
    "component.uses_components": "Afhankelijke Componenten",
    "component.uses_components.description": "Andere componenten waar dit component van afhankelijk is",
    # Repository form
    "repository.details": "Repository Details",
    "repository.name": "Repository Naam",
    "repository.name.description": "Identificerende naam voor de repository",
    "repository.url": "Repository URL",
    "repository.url.description": "Git URL (HTTPS of SSH)",
    "repository.username": "Gebruikersnaam",
    "repository.username.description": "Git gebruikersnaam",
    "repository.password": "Wachtwoord/Token",
    "repository.password.description": "Git wachtwoord of access token (versleuteld opgeslagen)",
    "repository.branch": "Branch",
    "repository.branch.description": "Git branch om te gebruiken",
    "repository.path": "Pad",
    "repository.path.description": "Pad binnen de repository",
    "repository.project_name": "Project Naam",
    "repository.project_name.description": "Naam van het project in de repository",
    # Deployment form
    "deployment.name": "Deployment Naam",
    "deployment.name.description": "Unieke naam voor deze deployment",
    "deployment.cluster": "Cluster",
    "deployment.cluster.description": "Kubernetes cluster voor deployment",
    "deployment.namespace": "Namespace",
    "deployment.namespace.description": "Kubernetes namespace",
    "deployment.repository": "Repository",
    "deployment.repository.description": "Broncode repository",
    "deployment.subdomain": "Subdomein",
    "deployment.subdomain.description": "Subdomein voor ingress URLs",
    "deployment.components": "Componenten",
    "deployment.components.description": "Componenten in deze deployment",
    "deployment.configuration": "Configuratie",
    "deployment.configuration.description": "Extra deployment configuratie",
    "deployment.component.reference": "Component Referentie",
    "deployment.component.reference.description": "Naam van het component",
    "deployment.component.image": "Container Image",
    "deployment.component.image.description": "Docker image voor dit component",
    "deployment.component.pull_policy": "Pull Policy",
    "deployment.component.pull_policy.description": "Image pull policy (Always, Never, IfNotPresent)",
}


def get_default_nl_translator() -> DictTranslator:
    """Get a translator with default Dutch translations."""
    return DictTranslator(DEFAULT_NL_TRANSLATIONS.copy())


def create_translator(
    language: str = "nl",
    translations: dict[str, str] | None = None,
) -> Translator:
    """
    Create a translator for the specified language.

    Args:
        language: Language code (e.g., "nl", "en")
        translations: Optional additional translations

    Returns:
        Translator instance
    """
    if language == "nl":
        translator = get_default_nl_translator()
        if translations:
            translator.update(translations)
        return translator

    # Default to identity translator for unsupported languages
    if translations:
        return DictTranslator(translations)
    return IdentityTranslator()
