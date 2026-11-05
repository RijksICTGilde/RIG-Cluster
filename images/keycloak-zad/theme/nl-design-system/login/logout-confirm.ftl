<#import "template.ftl" as layout>
<#import "nldd-commons.ftl" as nldd>
<@layout.registrationLayout; section>
    <#if section == "header">
        ${msg("logoutConfirmTitle")}
    <#elseif section == "form">
        <nldd-container id="kc-logout-confirm" gap="16">
            <@nldd.instruction>${msg("logoutConfirmHeader")}</@nldd.instruction>
            <nldd-form>
                <form action="${url.logoutConfirmAction}" method="post">
                    <input type="hidden" name="session_code" value="${logoutConfirm.code}"/>
                    <@nldd.actions submitText=msg("doLogout") submitId="kc-logout"/>
                </form>
            </nldd-form>
            <#if !logoutConfirm.skipLink>
                <@nldd.backToApplication/>
            </#if>
        </nldd-container>
    </#if>
</@layout.registrationLayout>
