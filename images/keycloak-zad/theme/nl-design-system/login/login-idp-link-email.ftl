<#import "template.ftl" as layout>
<#import "nldd-commons.ftl" as nldd>
<@layout.registrationLayout; section>
    <#if section == "header">
        ${msg("emailLinkIdpTitle", idpDisplayName)}
    <#elseif section == "form">
        <nldd-container gap="16">
            <@nldd.instruction>${msg("emailLinkIdp1", idpDisplayName, brokerContext.username, realm.displayName)}</@nldd.instruction>
            <@nldd.instruction>${msg("emailLinkIdp2")} <a href="${url.loginAction}">${msg("doClickHere")}</a> ${msg("emailLinkIdp3")}</@nldd.instruction>
            <@nldd.instruction>${msg("emailLinkIdp4")} <a href="${url.loginAction}">${msg("doClickHere")}</a> ${msg("emailLinkIdp5")}</@nldd.instruction>
        </nldd-container>
    </#if>
</@layout.registrationLayout>
