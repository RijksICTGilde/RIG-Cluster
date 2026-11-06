<#import "template.ftl" as layout>
<#import "nldd-commons.ftl" as nldd>
<@layout.registrationLayout displayMessage=!messagesPerField.existsError('password'); section>
    <#if section == "header">
        ${msg("doLogIn")}
    <#elseif section == "form">
        <nldd-form>
            <form id="kc-form-login" action="${url.loginAction}" method="post">
                <@nldd.field name="password" label=msg("password") type="password"
                             autocomplete="current-password" autofocus=true
                             error=messagesPerField.existsError('password')?then(kcSanitize(messagesPerField.get('password')), '')/>
                <#if realm.resetPasswordAllowed>
                    <nldd-link size="sm" href="${url.loginResetCredentialsUrl}"
                               text="${msg('doForgotPassword')}"></nldd-link>
                </#if>
                <@nldd.actions submitText=msg("doLogIn") submitId="kc-login"/>
            </form>
        </nldd-form>
    </#if>
</@layout.registrationLayout>
