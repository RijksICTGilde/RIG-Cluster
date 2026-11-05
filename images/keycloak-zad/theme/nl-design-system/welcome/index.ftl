<#--
  De welkomstpagina van de server. Hij hangt niet aan een realm, dus hij heeft geen
  berichtenbestand en geen taalkeuze: de teksten staan hier.

  De variabelen volgen keycloak/welcome/index.ftl van de serverversie. Keycloak 26.8 tekent
  daar niets meer zodra de beheerder bestaat; de uitleg onderaan is van ons.
-->
<!DOCTYPE html>
<html lang="nl">
  <head>
    <meta charset="utf-8">
    <meta name="robots" content="noindex, nofollow">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>${properties.organizationName}</title>
    <link rel="icon" href="${resourcesCommonPath}/nldd/favicon.svg">
    <#if properties.stylesCommon?has_content>
      <#list properties.stylesCommon?split(' ') as style>
        <link rel="stylesheet" href="${resourcesCommonPath}/${style}">
      </#list>
    </#if>
    <#-- `defer`: zie de uitleg bij dezelfde regel in login/template.ftl. -->
    <script src="${resourcesCommonPath}/nldd/nldd.min.js" defer></script>
  </head>
<body data-page-id="welcome">
<nldd-shell background="tinted">
  <nldd-page landmarks="page">
    <nldd-skip-link slot="header" text="Sla de navigatie over" href="#inhoud">
      <nldd-top-navigation-bar logo-title="${properties.organizationName}"
                               website-title="${properties.organizationName}">
        <#if adminConsoleEnabled>
          <nldd-menu-bar slot="utility" accessible-label="Beheer">
            <nldd-menu-bar-item href="${adminUrl}" icon="settings" content-priority="text"
                                text="Admin Console"></nldd-menu-bar-item>
          </nldd-menu-bar>
        </#if>
      </nldd-top-navigation-bar>
    </nldd-skip-link>

    <nldd-simple-section horizontal-alignment="center">
      <nldd-container id="inhoud" max-width="480px" gap="24">
        <#if adminConsoleEnabled && (bootstrap || successMessage?has_content)>
          <#if localUser>
            <nldd-title size="2" heading-level="1" text="Maak een beheerder aan"></nldd-title>
            <#if !successMessage?has_content>
              <nldd-text>Om te beginnen maak je eerst een beheerder aan.</nldd-text>
            </#if>
          <#else>
            <nldd-title size="2" heading-level="1" text="Lokale toegang vereist"></nldd-title>
            <nldd-text>Je hebt lokale toegang nodig om de beheerder aan te maken.</nldd-text>
          </#if>

          <#if successMessage?has_content>
            <nldd-banner variant="success" text="${successMessage}"></nldd-banner>
            <nldd-button href="${adminUrl}" appearance="primary" text="Open de Admin Console"></nldd-button>
          </#if>

          <#if bootstrap>
            <#if localUser>
              <#if errorMessage?has_content>
                <nldd-banner variant="critical" text="${errorMessage}"></nldd-banner>
              </#if>
              <nldd-form>
                <form method="post">
                  <nldd-form-field label="Gebruikersnaam">
                    <nldd-text-field id="username" name="username" autocomplete="username" required></nldd-text-field>
                  </nldd-form-field>
                  <nldd-form-field label="Wachtwoord">
                    <nldd-password-field id="password" name="password" autocomplete="new-password" required></nldd-password-field>
                  </nldd-form-field>
                  <nldd-form-field label="Wachtwoordbevestiging">
                    <nldd-password-field id="password-confirmation" name="passwordConfirmation"
                                         autocomplete="new-password" required></nldd-password-field>
                  </nldd-form-field>
                  <nldd-form-field label="E-mailadres" optional>
                    <nldd-text-field id="email" name="email" type="email" autocomplete="email"></nldd-text-field>
                  </nldd-form-field>
                  <nldd-form-field label="Voornaam" optional>
                    <nldd-text-field id="firstName" name="firstName" autocomplete="given-name"></nldd-text-field>
                  </nldd-form-field>
                  <nldd-form-field label="Achternaam" optional>
                    <nldd-text-field id="lastName" name="lastName" autocomplete="family-name"></nldd-text-field>
                  </nldd-form-field>
                  <input type="hidden" name="stateChecker" value="${stateChecker}">
                  <nldd-form-actions>
                    <nldd-button-group>
                      <nldd-button type="submit" appearance="primary" width="full"
                                   text="Maak de beheerder aan"></nldd-button>
                    </nldd-button-group>
                  </nldd-form-actions>
                </form>
              </nldd-form>
            <#else>
              <nldd-rich-text>
                <p>Open de Admin Console via localhost om de beheerder aan te maken, of gebruik het commando <code>bootstrap-admin</code>.</p>
              </nldd-rich-text>
            </#if>
          </#if>
        <#else>
          <nldd-title size="2" heading-level="1" text="Welkom"></nldd-title>
          <nldd-text size="lg">Dit is een identity service. Via deze service log je in op verschillende applicaties.</nldd-text>
          <nldd-rich-text>
            <p>De Admin Console is alleen voor beheerders. Als gebruiker heb je de naam van je eigen <code>realm</code> nodig.</p>
            <p>Met die naam ga je naar de inlog-URL, bijvoorbeeld <code>${baseUrl}/realms/jerealm/account</code>, waarbij je <code>jerealm</code> vervangt door de naam van je realm.</p>
          </nldd-rich-text>
        </#if>
      </nldd-container>
    </nldd-simple-section>
  </nldd-page>
</nldd-shell>
</body>
</html>
