<#import "template.ftl" as layout>
<#import "nldd-commons.ftl" as nldd>
<@layout.registrationLayout; section>
    <#if section == "header">
        ${msg("pageExpiredTitle")}
    <#elseif section == "form">
        <nldd-container gap="16">
            <@nldd.instruction>${msg("pageExpiredMsg1")}</@nldd.instruction>
            <nldd-link id="loginRestartLink" size="sm" href="${url.loginRestartFlowUrl}"
                       start-icon="arrow-2-counter-clockwise" text="${msg('doClickHere')}"></nldd-link>
            <@nldd.instruction>${msg("pageExpiredMsg2")}</@nldd.instruction>
            <nldd-link id="loginContinueLink" size="sm" href="${url.loginAction}"
                       start-icon="arrow-right" text="${msg('doClickHere')}"></nldd-link>
        </nldd-container>
    </#if>
</@layout.registrationLayout>
