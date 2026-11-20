<#import "template.ftl" as layout>
<#import "nldd-commons.ftl" as nldd>

<#-- Het keuzescherm van de storingsstand: de melding over de storing, inloggen via
     e-mail, en de knop naar SSO Rijk die op 22 oktober 2026 nog wel werkte vanuit Citrix.
     Zie features/sso-rijk-uitwijk.md. -->
<@layout.registrationLayout displayMessage=true displayInfo=false; section>
    <#if section == "header">
        ${msg("loginAccountTitle")}
    <#elseif section == "form">
        <nldd-container gap="24">
            <nldd-banner variant="warning" text="${msg('zadMagicLinkOutageTitle')}"
                         supporting-text="${msg('zadMagicLinkOutageText')}"></nldd-banner>

            <nldd-form>
                <form id="kc-magic-link-form" action="${url.loginAction}" method="post">
                    <@nldd.instruction>${msg("zadMagicLinkEmailInstruction")}</@nldd.instruction>
                    <@nldd.field name="email" label=msg("email") type="email"
                                 value=(zadMagicLinkEmail!'')
                                 autocomplete="email" autofocus=true required=true/>
                    <@nldd.actions submitText=msg("zadMagicLinkSend") submitId="kc-magic-link-send"/>
                </form>
            </nldd-form>
        </nldd-container>
    <#elseif section == "socialProviders">
        <#if social?? && social.providers?has_content>
            <nldd-container id="kc-social-providers" gap="8">
                <#list social.providers as provider>
                    <nldd-button id="social-${provider.alias}"
                                 href="${provider.loginUrl}"
                                 width="full"
                                 appearance="secondary"
                                 text="${msg('zadMagicLinkTryIdp', provider.displayName!provider.alias)}"></nldd-button>
                </#list>
            </nldd-container>
        </#if>
    </#if>
</@layout.registrationLayout>
