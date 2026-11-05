<#import "template.ftl" as layout>
<#import "nldd-commons.ftl" as nldd>
<@layout.registrationLayout displayMessage=false; section>
    <#if section == "header">
        <#if messageHeader??>${kcSanitize(msg("${messageHeader}"))?no_esc}<#else>${message.summary}</#if>
    <#elseif section == "form">
        <nldd-container id="kc-info-message" gap="16">
            <@nldd.instruction>
                ${message.summary}<#if requiredActions??><#list requiredActions>: <strong><#items as reqActionItem>${kcSanitize(msg("requiredAction.${reqActionItem}"))?no_esc}<#sep>, </#items></strong></#list></#if>
            </@nldd.instruction>
            <#if !skipLink??>
                <#if pageRedirectUri?has_content>
                    <nldd-link size="sm" href="${pageRedirectUri}" start-icon="arrow-left"
                               text="${msg('backToApplication')}"></nldd-link>
                <#elseif actionUri?has_content>
                    <nldd-link size="sm" href="${actionUri}" start-icon="arrow-right"
                               text="${msg('proceedWithAction')}"></nldd-link>
                <#else>
                    <@nldd.backToApplication/>
                </#if>
            </#if>
        </nldd-container>
    </#if>
</@layout.registrationLayout>
