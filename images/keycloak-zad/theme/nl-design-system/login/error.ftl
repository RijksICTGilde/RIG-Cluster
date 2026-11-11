<#import "template.ftl" as layout>
<#import "nldd-commons.ftl" as nldd>

<#-- `accessDeniedNoPermission` komt van de RequireClientRoleAuthenticator en krijgt een
     eigen pagina die zegt hoe je toegang krijgt; zie features/client-access-restriction.md. -->
<#assign isAccessDenied = false>
<#if message?? && message.summary??>
    <#if message.summary?starts_with("$") && message.summary?ends_with("}")>
        <#assign msgKey = message.summary?substring(2, message.summary?length - 1)>
        <#if msgKey == "accessDeniedNoPermission">
            <#assign isAccessDenied = true>
        </#if>
    <#elseif message.summary == "Access denied">
        <#assign isAccessDenied = true>
    </#if>
</#if>

<@layout.registrationLayout displayMessage=false; section>
    <#if section == "header">
        <#if isAccessDenied>${msg("accessRestrictedTitle")}<#else>${msg("errorTitle")}</#if>
    <#elseif section == "form">
        <#if isAccessDenied>
            <nldd-container gap="24">
                <nldd-banner variant="accent" text="${msg('accessRestrictedHeading')}"
                             supporting-text="${msg('accessRestrictedDescription')}"></nldd-banner>
                <nldd-title size="4" heading-level="2" text="${msg('accessRestrictedHowTo')}"></nldd-title>
                <nldd-rich-text>
                    <ol>
                        <li>${msg("accessRestrictedStep1")}</li>
                        <li>${msg("accessRestrictedStep2")}</li>
                        <li>${msg("accessRestrictedStep3")}</li>
                    </ol>
                </nldd-rich-text>
                <@nldd.backToApplication/>
            </nldd-container>
        <#else>
            <nldd-container gap="24">
                <#-- Zie template.ftl: de gesaniteerde melding hoort in de inhoud. -->
                <nldd-banner variant="critical">
                    <nldd-rich-text><p>${kcSanitize(message.summary)?no_esc}</p></nldd-rich-text>
                </nldd-banner>
                <@nldd.backToApplication/>
            </nldd-container>
        </#if>
    </#if>
</@layout.registrationLayout>
