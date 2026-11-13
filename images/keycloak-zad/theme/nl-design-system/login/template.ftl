<#--
  De paginaschil van elke loginpagina: de balk met het Rijkslogo en de taalkeuze, de titel,
  de melding van Keycloak, en daaronder wat de pagina zelf levert.

  De NLDD-bundel is een IIFE met een globale `NLDD`, dus geen type="module": zo laadt hij
  ook op een pagina die zelf geen module heeft.
-->
<#macro registrationLayout bodyClass="" displayInfo=false displayMessage=true displayRequiredFields=false>
<!DOCTYPE html>
<html<#if realm.internationalizationEnabled> lang="${locale.currentLanguageTag}" dir="${(locale.rtl)?then('rtl','ltr')}"</#if>>
<head>
    <meta charset="utf-8">
    <meta name="robots" content="noindex, nofollow">

    <#if properties.meta?has_content>
        <#list properties.meta?split(' ') as meta>
            <meta name="${meta?split('==')[0]}" content="${meta?split('==')[1]}"/>
        </#list>
    </#if>
    <title>${msg("doLogIn")} ${properties.organizationName} - ${realm.displayName!''}</title>
    <link rel="icon" href="${url.resourcesCommonPath}/nldd/favicon.svg" />
    <#if properties.stylesCommon?has_content>
        <#list properties.stylesCommon?split(' ') as style>
            <link href="${url.resourcesCommonPath}/${style}" rel="stylesheet" />
        </#list>
    </#if>
    <#if properties.styles?has_content>
        <#list properties.styles?split(' ') as style>
            <link href="${url.resourcesPath}/${style}" rel="stylesheet" />
        </#list>
    </#if>
    <#-- `defer`, dus na het parsen van de hele pagina. Zonder dit bouwt elk component zich op
         op het moment dat zijn eigen tag langskomt, dus nog zonder kinderen, en meet
         nldd-menu-bar zichzelf als leeg: de taalkeuze bleef dan weg (gemeten: 7 van de 12
         ladingen). Het module-script hieronder draait daarna, dus `NLDD` bestaat dan. -->
    <script src="${url.resourcesCommonPath}/nldd/nldd.min.js" defer></script>
    <#-- De componenten spreken standaard Nederlands; op een andere taal komen hun eigen
         teksten (de knop die het wachtwoord toont, bijvoorbeeld) uit en-US.js. -->
    <#if realm.internationalizationEnabled && locale.currentLanguageTag != "nl">
        <script type="module">
            <#outputformat "JavaScript">
            import { enUS } from ${(url.resourcesCommonPath + "/nldd/translations/en-US.js")?c};
            NLDD.setTranslations(enUS);
            </#outputformat>
        </script>
    </#if>
    <#if scripts??>
        <#list scripts as script>
            <script src="${script}" type="text/javascript"></script>
        </#list>
    </#if>
    <script type="module">
        <#outputformat "JavaScript">
        import { startSessionPolling } from ${(url.resourcesPath + "/js/authChecker.js")?c};

        startSessionPolling(${url.ssoLoginInOtherTabsUrl?c});
        </#outputformat>
    </script>
    <#if authenticationSession??>
        <script type="module">
            <#outputformat "JavaScript">
            import { checkAuthSession } from ${(url.resourcesPath + "/js/authChecker.js")?c};

            checkAuthSession(${authenticationSession.authSessionIdHash?c});
            </#outputformat>
        </script>
    </#if>
</head>

<body data-page-id="login-${pageId}">
<nldd-shell background="tinted">
  <nldd-page landmarks="page">
    <nldd-skip-link slot="header" text="${msg('skipToContent')}" href="#kc-content">
      <nldd-top-navigation-bar logo-title="${properties.organizationName}"
                               website-title="${realm.displayName!''}">
        <#if realm.internationalizationEnabled && locale.supported?size gt 1>
          <nldd-menu-bar slot="utility" accessible-label="${msg('languages')}">
            <nldd-menu-bar-item id="kc-current-locale-link" expandable content-priority="text"
                                text="${locale.current}"
                                accessible-label="${msg('languages')}: ${locale.current}">
              <nldd-menu variant="navigation" accessible-label="${msg('languages')}">
                <#list locale.supported as l>
                  <nldd-menu-item href="${l.url}" text="${l.label}" lang="${l.languageTag}"></nldd-menu-item>
                </#list>
              </nldd-menu>
            </nldd-menu-bar-item>
          </nldd-menu-bar>
        </#if>
      </nldd-top-navigation-bar>
    </nldd-skip-link>

    <nldd-simple-section horizontal-alignment="center">
      <nldd-container id="kc-content" max-width="480px" gap="24">
        <#if displayRequiredFields>
          <nldd-text size="sm" color="secondary">${msg("requiredFields")}</nldd-text>
        </#if>

        <nldd-title size="2" heading-level="1"<#if auth?has_content && auth.showUsername() && !auth.showResetCredentials()> supporting-text="${auth.attemptedUsername}"</#if>>
          <h1 id="kc-page-title"><#nested "header"></h1>
          <#if auth?has_content && auth.showUsername() && !auth.showResetCredentials()>
            <nldd-link slot="end" size="sm" id="reset-login" href="${url.loginRestartFlowUrl}"
                       start-icon="arrow-2-counter-clockwise"
                       text="${msg('restartLoginTooltip')}"></nldd-link>
          </#if>
        </nldd-title>
        <#if auth?has_content && auth.showUsername() && !auth.showResetCredentials()>
          <#nested "show-username">
        </#if>

        <#if displayMessage && message?has_content && (message.type != 'warning' || !isAppInitiatedAction??)>
          <#-- De melding staat in de INHOUD en niet in `text`, zoals upstream het ook doet:
               kcSanitize laat opmaak staan, en in een attribuutwaarde komt die als
               letterlijke tekst in beeld. -->
          <nldd-banner variant="<#if message.type = 'error'>critical<#elseif message.type = 'warning'>warning<#elseif message.type = 'success'>success<#else>accent</#if>">
            <nldd-rich-text><p>${kcSanitize(message.summary)?no_esc}</p></nldd-rich-text>
          </nldd-banner>
        </#if>

        <#nested "socialProviders">
        <#nested "form">

        <#if auth?has_content && auth.showTryAnotherWayLink()>
          <nldd-form>
            <form id="kc-select-try-another-way-form" action="${url.loginAction}" method="post">
              <input type="hidden" name="tryAnotherWay" value="on"/>
              <nldd-form-actions>
                <nldd-button-group>
                  <nldd-button id="try-another-way" type="submit" appearance="secondary"
                               text="${msg('doTryAnotherWay')}"></nldd-button>
                </nldd-button-group>
              </nldd-form-actions>
            </form>
          </nldd-form>
        </#if>

        <#if displayInfo>
          <#nested "info">
        </#if>
      </nldd-container>
    </nldd-simple-section>
  </nldd-page>
</nldd-shell>
</body>
</html>
</#macro>
