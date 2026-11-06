<#import "template.ftl" as layout>
<#import "nldd-commons.ftl" as nldd>
<@layout.registrationLayout displayRequiredFields=false displayMessage=!messagesPerField.existsError('totp','userLabel'); section>
    <#if section == "header">
        ${msg("loginTotpTitle")}
    <#elseif section == "form">
        <nldd-container gap="16">
            <nldd-title size="5" heading-level="2" text="${msg('loginTotpStep1')}"></nldd-title>
            <nldd-rich-text>
                <ul>
                    <#list totp.supportedApplications as app>
                        <li>${msg(app)}</li>
                    </#list>
                </ul>
            </nldd-rich-text>

            <#if mode?? && mode == "manual">
                <nldd-title size="5" heading-level="2" text="${msg('loginTotpManualStep2')}"></nldd-title>
                <nldd-box>
                    <nldd-container padding="16" gap="8">
                        <nldd-text id="kc-totp-secret-key" weight="bold">${totp.totpSecretEncoded}</nldd-text>
                        <nldd-link size="sm" id="mode-barcode" href="${totp.qrUrl}"
                                   text="${msg('loginTotpScanBarcode')}"></nldd-link>
                    </nldd-container>
                </nldd-box>
                <nldd-title size="5" heading-level="2" text="${msg('loginTotpManualStep3')}"></nldd-title>
                <nldd-rich-text>
                    <ul>
                        <li id="kc-totp-type">${msg("loginTotpType")}: ${msg("loginTotp." + totp.policy.type)}</li>
                        <li id="kc-totp-algorithm">${msg("loginTotpAlgorithm")}: ${totp.policy.getAlgorithmKey()}</li>
                        <li id="kc-totp-digits">${msg("loginTotpDigits")}: ${totp.policy.digits}</li>
                        <#if totp.policy.type == "totp">
                            <li id="kc-totp-period">${msg("loginTotpInterval")}: ${totp.policy.period}</li>
                        <#elseif totp.policy.type == "hotp">
                            <li id="kc-totp-counter">${msg("loginTotpCounter")}: ${totp.policy.initialCounter}</li>
                        </#if>
                    </ul>
                </nldd-rich-text>
            <#else>
                <nldd-title size="5" heading-level="2" text="${msg('loginTotpStep2')}"></nldd-title>
                <img id="kc-totp-secret-qr-code" width="200" height="200"
                     src="data:image/png;base64, ${totp.totpSecretQrCode}" alt="${msg('loginTotpStep2')}"/>
                <nldd-link size="sm" id="mode-manual" href="${totp.manualUrl}"
                           text="${msg('loginTotpUnableToScan')}"></nldd-link>
            </#if>

            <nldd-title size="5" heading-level="2" text="${msg('loginTotpStep3')}"></nldd-title>
            <@nldd.instruction>${msg("loginTotpStep3DeviceName")}</@nldd.instruction>
        </nldd-container>

        <nldd-form>
            <form id="kc-totp-settings-form" action="${url.loginAction}" method="post">
                <@nldd.field name="totp" label=msg("authenticatorCode") autocomplete="one-time-code"
                             required=true inputmode="numeric"
                             error=messagesPerField.existsError('totp')?then(kcSanitize(messagesPerField.get('totp')), '')/>
                <@nldd.field name="userLabel" label=msg("loginTotpDeviceName")
                             required=(totp.otpCredentials?size gte 1)
                             error=messagesPerField.existsError('userLabel')?then(kcSanitize(messagesPerField.get('userLabel')), '')/>
                <input type="hidden" id="totpSecret" name="totpSecret" value="${totp.totpSecret}"/>
                <#if mode??><input type="hidden" id="mode" name="mode" value="${mode}"/></#if>
                <@nldd.logoutOtherSessions/>
                <@nldd.actions submitText=msg("doSubmit") submitId="saveTOTPBtn"/>
            </form>
        </nldd-form>
        <#if isAppInitiatedAction??>
            <@nldd.cancelAia action=url.loginAction/>
        </#if>
    </#if>
</@layout.registrationLayout>
