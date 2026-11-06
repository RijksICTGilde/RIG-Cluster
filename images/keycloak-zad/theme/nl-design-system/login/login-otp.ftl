<#import "template.ftl" as layout>
<#import "nldd-commons.ftl" as nldd>
<@layout.registrationLayout displayMessage=!messagesPerField.existsError('totp'); section>
    <#if section == "header">
        ${msg("doLogIn")}
    <#elseif section == "form">
        <nldd-form>
            <form id="kc-otp-login-form" action="${url.loginAction}" method="post">
                <#if otpLogin.userOtpCredentials?size gt 1>
                    <nldd-form-field label="${msg('loginChooseAuthenticator')}">
                        <nldd-radio-button-group name="selectedCredentialId">
                            <#list otpLogin.userOtpCredentials as otpCredential>
                                <nldd-radio-button-field id="kc-otp-credential-${otpCredential?index}"
                                                         value="${otpCredential.id}"
                                                         label="${otpCredential.userLabel}"
                                                         <#if otpCredential.id == otpLogin.selectedCredentialId>checked</#if>></nldd-radio-button-field>
                            </#list>
                        </nldd-radio-button-group>
                    </nldd-form-field>
                </#if>
                <@nldd.field name="otp" label=msg("loginOtpOneTime") autocomplete="one-time-code"
                             autofocus=true inputmode="numeric"
                             error=messagesPerField.existsError('totp')?then(kcSanitize(messagesPerField.get('totp')), '')/>
                <@nldd.actions submitText=msg("doLogIn") submitId="kc-login"/>
            </form>
        </nldd-form>
    </#if>
</@layout.registrationLayout>
