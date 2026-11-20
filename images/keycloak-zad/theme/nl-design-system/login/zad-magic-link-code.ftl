<#import "template.ftl" as layout>
<#import "nldd-commons.ftl" as nldd>

<#-- De tweede stap: de code uit de mail overtypen, of een nieuwe aanvragen. Het scherm is
     hetzelfde voor een bekend en een onbekend e-mailadres. -->
<@layout.registrationLayout displayMessage=true displayInfo=false; section>
    <#if section == "header">
        ${msg("zadMagicLinkCodeTitle")}
    <#elseif section == "form">
        <nldd-container gap="24">
            <nldd-form>
                <form id="kc-magic-link-code-form" action="${url.loginAction}" method="post">
                    <@nldd.instruction>${msg("zadMagicLinkCodeInstruction", zadMagicLinkValidMinutes)}</@nldd.instruction>
                    <@nldd.field name="code" label=msg("zadMagicLinkCodeLabel")
                                 autocomplete="one-time-code" autofocus=true required=true/>
                    <@nldd.actions submitText=msg("doLogIn") submitId="kc-magic-link-verify"/>
                </form>
            </nldd-form>

            <@nldd.choiceForm action=url.loginAction name="resend" value="true"
                              id="kc-magic-link-resend"
                              text=msg("zadMagicLinkResend")/>
        </nldd-container>
    </#if>
</@layout.registrationLayout>
