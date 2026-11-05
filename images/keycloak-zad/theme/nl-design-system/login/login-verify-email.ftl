<#import "template.ftl" as layout>
<#import "nldd-commons.ftl" as nldd>
<@layout.registrationLayout displayInfo=true; section>
    <#if section == "header">
        ${msg("emailVerifyTitle")}
    <#elseif section == "form">
        <@nldd.instruction>
            <#if verifyEmail??>${msg("emailVerifyInstruction1",verifyEmail)}<#else>${msg("emailVerifyInstruction4",user.email)}</#if>
        </@nldd.instruction>
        <#if isAppInitiatedAction??>
            <nldd-form>
                <form id="kc-verify-email-form" action="${url.loginAction}" method="post">
                    <@nldd.actions submitText=(verifyEmail??)?then(msg("emailVerifyResend"), msg("emailVerifySend"))/>
                </form>
            </nldd-form>
            <@nldd.cancelAia action=url.loginAction/>
        </#if>
    <#elseif section == "info">
        <#if !isAppInitiatedAction??>
            <@nldd.instruction>
                ${msg("emailVerifyInstruction2")}
                <a href="${url.loginAction}">${msg("doClickHere")}</a> ${msg("emailVerifyInstruction3")}
            </@nldd.instruction>
        </#if>
    </#if>
</@layout.registrationLayout>
