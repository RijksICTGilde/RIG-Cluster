<#import "template.ftl" as layout>
<#import "nldd-commons.ftl" as nldd>
<@layout.registrationLayout displayMessage=!messagesPerField.existsError('username','password') displayInfo=realm.password && realm.registrationAllowed && !registrationDisabled??; section>
    <#if section == "header">
        ${msg("loginAccountTitle")}
    <#elseif section == "form">
        <#if realm.password>
            <#assign loginError = messagesPerField.existsError('username','password')?then(kcSanitize(messagesPerField.getFirstError('username','password')), '')>
            <#if social?? && social.providers?has_content>
                <nldd-divider></nldd-divider>
                <nldd-title size="5" heading-level="2" text="${msg('identity-provider-login-label')}"></nldd-title>
            </#if>
            <nldd-form>
                <form id="kc-form-login" action="${url.loginAction}" method="post">
                    <#if !usernameHidden??>
                        <@nldd.field name="username"
                                     label=realm.loginWithEmailAllowed?then(realm.registrationEmailAsUsername?then(msg("email"), msg("usernameOrEmail")), msg("username"))
                                     value=(login.username!'')
                                     autocomplete="username"
                                     autofocus=true
                                     error=loginError/>
                    </#if>
                    <@nldd.field name="password" label=msg("password") type="password"
                                 autocomplete="current-password"
                                 error=(usernameHidden??)?then(loginError, '')/>
                    <#if realm.resetPasswordAllowed>
                        <nldd-link size="sm" href="${url.loginResetCredentialsUrl}"
                                   text="${msg('doForgotPassword')}"></nldd-link>
                    </#if>
                    <#if realm.rememberMe && !usernameHidden??>
                        <nldd-checkbox-field id="rememberMe" name="rememberMe" value="on"
                                             label="${msg('rememberMe')}"
                                             <#if login.rememberMe??>checked</#if>></nldd-checkbox-field>
                    </#if>
                    <input type="hidden" id="id-hidden-input" name="credentialId" <#if auth.selectedCredential?has_content>value="${auth.selectedCredential}"</#if>/>
                    <@nldd.actions submitText=msg("doLogIn") submitId="kc-login"/>
                </form>
            </nldd-form>
        </#if>
    <#elseif section == "info">
        <#if realm.password && realm.registrationAllowed && !registrationDisabled??>
            <nldd-banner variant="neutral" text="${msg('noAccount')}">
                <nldd-button slot="actions" appearance="accent-transparent" href="${url.registrationUrl}" text="${msg('doRegister')}"></nldd-button>
            </nldd-banner>
        </#if>
    <#elseif section == "socialProviders">
        <#if realm.password && social?? && social.providers?has_content>
            <nldd-container id="kc-social-providers" gap="8">
                <#list social.providers as provider>
                    <nldd-button id="social-${provider.alias}"
                                 href="${provider.loginUrl}"
                                 width="full"
                                 appearance="${(provider?index == 0)?then('primary', 'secondary')}"
                                 text="${provider.displayName!provider.alias}"></nldd-button>
                </#list>
            </nldd-container>
        </#if>
    </#if>
</@layout.registrationLayout>
