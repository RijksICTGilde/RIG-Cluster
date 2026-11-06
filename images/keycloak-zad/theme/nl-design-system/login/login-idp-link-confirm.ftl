<#import "template.ftl" as layout>
<#import "nldd-commons.ftl" as nldd>
<@layout.registrationLayout; section>
    <#if section == "header">
        ${msg("confirmLinkIdpTitle")}
    <#elseif section == "form">
        <nldd-container gap="8">
            <#if !hideReviewButton?has_content>
                <@nldd.choiceForm action=url.loginAction name="submitAction" value="updateProfile"
                                  id="updateProfile" text=msg("confirmLinkIdpReviewProfile")/>
            </#if>
            <@nldd.choiceForm action=url.loginAction name="submitAction" value="linkAccount"
                              id="linkAccount" appearance="primary"
                              text=msg("confirmLinkIdpContinue", idpDisplayName)/>
        </nldd-container>
    </#if>
</@layout.registrationLayout>
