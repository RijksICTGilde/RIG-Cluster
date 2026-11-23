<#import "template.ftl" as layout>
<#import "nldd-commons.ftl" as nldd>

<#-- De pagina achter de link uit de mail. Het openen van de link logt niet in: mailscanners
     openen een link vooraf, en een link die daarop inlogt is verbruikt voordat de gebruiker
     hem ziet. Pas de POST van deze knop logt in, en die gaat naar de browser-flow: het
     action-token-endpoint van Keycloak neemt alleen een GET. -->
<@layout.registrationLayout displayMessage=true displayInfo=false; section>
    <#if section == "header">
        ${msg("zadMagicLinkConfirmTitle")}
    <#elseif section == "form">
        <nldd-container gap="24">
            <@nldd.instruction>${msg("zadMagicLinkConfirmInstruction")}</@nldd.instruction>
            <@nldd.choiceForm action=url.loginAction name="confirm" value="true"
                              id="kc-magic-link-confirm"
                              appearance="primary"
                              text=msg("doLogIn")/>
        </nldd-container>
    </#if>
</@layout.registrationLayout>
