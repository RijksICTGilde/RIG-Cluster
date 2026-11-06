<#import "template.ftl" as layout>
<#import "nldd-commons.ftl" as nldd>
<#import "user-profile-commons.ftl" as userProfileCommons>
<@layout.registrationLayout displayMessage=messagesPerField.exists('global') displayRequiredFields=true; section>
    <#if section == "header">
        <#if messageHeader??>${kcSanitize(msg("${messageHeader}"))?no_esc}<#else>${msg("registerTitle")}</#if>
    <#elseif section == "form">
        <nldd-form>
            <form id="kc-register-form" action="${url.registrationAction}" method="post">
                <@userProfileCommons.userProfileFormFields; callback, attribute>
                    <#if callback == "afterField" && passwordRequired?? && (attribute.name == 'username' || (attribute.name == 'email' && realm.registrationEmailAsUsername))>
                        <@nldd.field name="password" label=msg("password") type="password"
                                     autocomplete="new-password" required=true
                                     error=messagesPerField.existsError('password')?then(kcSanitize(messagesPerField.get('password')), '')/>
                        <@nldd.field name="password-confirm" label=msg("passwordConfirm") type="password"
                                     autocomplete="new-password" required=true
                                     error=messagesPerField.existsError('password-confirm')?then(kcSanitize(messagesPerField.get('password-confirm')), '')/>
                    </#if>
                </@userProfileCommons.userProfileFormFields>

                <#if termsAcceptanceRequired??>
                    <nldd-form-section text="${msg('termsTitle')}">
                        <nldd-rich-text id="kc-registration-terms-text">${kcSanitize(msg("termsText"))?no_esc}</nldd-rich-text>
                        <nldd-checkbox-field id="termsAccepted" name="termsAccepted" value="on"
                                             label="${msg('acceptTerms')}" required
                                             <#if messagesPerField.existsError('termsAccepted')>invalid</#if>></nldd-checkbox-field>
                    </nldd-form-section>
                </#if>

                <@nldd.actions submitText=msg("doRegister")/>
            </form>
        </nldd-form>
        <@nldd.backToLogin/>
    </#if>
</@layout.registrationLayout>
