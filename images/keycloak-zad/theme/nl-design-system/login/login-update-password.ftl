<#import "template.ftl" as layout>
<#import "nldd-commons.ftl" as nldd>
<@layout.registrationLayout displayMessage=!messagesPerField.existsError('password','password-confirm'); section>
    <#if section == "header">
        ${msg("updatePasswordTitle")}
    <#elseif section == "form">
        <nldd-form>
            <form id="kc-passwd-update-form" action="${url.loginAction}" method="post">
                <@nldd.field name="password-new" label=msg("passwordNew") type="password"
                             autocomplete="new-password" autofocus=true
                             error=messagesPerField.existsError('password')?then(kcSanitize(messagesPerField.get('password')), '')/>
                <@nldd.field name="password-confirm" label=msg("passwordConfirm") type="password"
                             autocomplete="new-password"
                             error=messagesPerField.existsError('password-confirm')?then(kcSanitize(messagesPerField.get('password-confirm')), '')/>
                <@nldd.logoutOtherSessions/>
                <@nldd.actions submitText=msg("doSubmit")/>
            </form>
        </nldd-form>
        <#if isAppInitiatedAction??>
            <@nldd.cancelAia action=url.loginAction/>
        </#if>
    </#if>
</@layout.registrationLayout>
