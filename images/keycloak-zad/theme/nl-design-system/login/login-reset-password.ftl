<#import "template.ftl" as layout>
<#import "nldd-commons.ftl" as nldd>
<@layout.registrationLayout displayInfo=true displayMessage=!messagesPerField.existsError('username'); section>
    <#if section == "header">
        ${msg("emailForgotTitle")}
    <#elseif section == "form">
        <@nldd.instruction>
            <#if realm.duplicateEmailsAllowed>${msg("emailInstructionUsername")}<#else>${msg("emailInstruction")}</#if>
        </@nldd.instruction>
        <nldd-form>
            <form id="kc-reset-password-form" action="${url.loginAction}" method="post">
                <@nldd.field name="username"
                             label=realm.loginWithEmailAllowed?then(realm.registrationEmailAsUsername?then(msg("email"), msg("usernameOrEmail")), msg("username"))
                             value=(auth.attemptedUsername!'')
                             autocomplete="username"
                             autofocus=true
                             error=messagesPerField.existsError('username')?then(kcSanitize(messagesPerField.get('username')), '')/>
                <@nldd.actions submitText=msg("doSubmit")/>
            </form>
        </nldd-form>
    <#elseif section == "info">
        <@nldd.backToLogin/>
    </#if>
</@layout.registrationLayout>
