<#import "template.ftl" as layout>
<#import "nldd-commons.ftl" as nldd>
<#import "user-profile-commons.ftl" as userProfileCommons>
<@layout.registrationLayout displayMessage=messagesPerField.exists('global') displayRequiredFields=true; section>
    <#if section == "header">
        ${msg("loginProfileTitle")}
    <#elseif section == "form">
        <nldd-form>
            <form id="kc-update-profile-form" action="${url.loginAction}" method="post">
                <@userProfileCommons.userProfileFormFields/>
                <@nldd.actions submitText=msg("doSubmit")/>
            </form>
        </nldd-form>
        <#if isAppInitiatedAction??>
            <@nldd.cancelAia action=url.loginAction/>
        </#if>
    </#if>
</@layout.registrationLayout>
