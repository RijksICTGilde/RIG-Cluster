<#import "template.ftl" as layout>
<#import "nldd-commons.ftl" as nldd>
<@layout.registrationLayout displayInfo=false; section>
    <#if section == "header">
        ${msg("loginChooseAuthenticator")}
    <#elseif section == "form">
        <#-- Elke keuze is zijn eigen formulier: nldd-button draagt geen name en geen value,
             dus de gekozen execution gaat als verborgen veld mee. -->
        <nldd-container id="kc-select-credential-form" gap="8">
            <#list auth.authenticationSelections as authenticationSelection>
                <@nldd.choiceForm action=url.loginAction
                                  name="authenticationExecution"
                                  value=authenticationSelection.authExecId
                                  text=msg('${authenticationSelection.displayName}')
                                  supportingText=msg('${authenticationSelection.helpText}')
                                  alignment="left"/>
            </#list>
        </nldd-container>
    </#if>
</@layout.registrationLayout>
