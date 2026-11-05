<#--
  De bouwstenen die de pagina's van dit thema delen. Hier bij elkaar omdat anders elke
  pagina hetzelfde nest van nldd-form-field, nldd-validation-list en nldd-form-actions
  herhaalt, en een wijziging in de opbouw negentien bestanden raakt.

  Een serverfout op een veld gaat via `unmet` op het invoerveld en een item met dat id in de
  validatielijst: zo kiest NLDD zelf het moment waarop hij de melding laat staan of weghaalt.

  nldd-button draagt geen name en geen value, dus een knop die een benoemde waarde moet
  posten (cancel-aia, submitAction, authenticationExecution) krijgt zijn eigen formulier met
  die waarde in een verborgen veld. Dat is `choiceForm`.
-->

<#-- Een tekst-, e-mail- of wachtwoordveld met zijn label en zijn eventuele foutmelding. -->
<#macro field name label type="text" value="" autocomplete="" autofocus=false required=false error="" inputmode="">
    <#local errorId = "error-" + name>
    <#local tag = (type == "password")?then("nldd-password-field", "nldd-text-field")>
    <nldd-form-field label="${label}">
        <${tag} id="${name}" name="${name}"<#rt>
            <#if type != "password" && type != "text"> type="${type}"</#if><#t>
            <#if inputmode?has_content> keyboard="${inputmode}"</#if><#t>
            <#if value?has_content> value="${value}"</#if><#t>
            <#if autocomplete?has_content> autocomplete="${autocomplete}"</#if><#t>
            <#if autofocus> autofocus</#if><#t>
            <#if required> required</#if><#t>
            <#if error?has_content> invalid unmet="${errorId}"</#if>></${tag}><#lt>
        <#if error?has_content>
            <nldd-validation-list>
                <nldd-validation-item id="${errorId}">${error?no_esc}</nldd-validation-item>
            </nldd-validation-list>
        </#if>
    </nldd-form-field>
</#macro>

<#-- De knoppenrij onder een formulier. -->
<#macro actions submitText submitId="kc-submit">
    <nldd-form-actions>
        <nldd-button-group>
            <nldd-button id="${submitId}" type="submit" appearance="primary" width="full"
                         text="${submitText}"></nldd-button>
        </nldd-button-group>
    </nldd-form-actions>
</#macro>

<#-- Eén keuze als eigen formulier: de waarde zit in een verborgen veld. -->
<#macro choiceForm action name value text id="" appearance="secondary" supportingText="" alignment="center">
    <nldd-form>
        <form action="${action}" method="post">
            <input type="hidden" name="${name}" value="${value}"/>
            <nldd-button<#if id?has_content> id="${id}"</#if> type="submit"
                         appearance="${appearance}" width="full"
                         horizontal-alignment="${alignment}"
                         <#if supportingText?has_content>supporting-text="${supportingText}"</#if>
                         text="${text}"></nldd-button>
        </form>
    </nldd-form>
</#macro>

<#-- De annuleerknop van een app-initiated action, als eigen formulier. -->
<#macro cancelAia action>
    <@choiceForm action=action name="cancel-aia" value="true" text=msg("doCancel")/>
</#macro>

<#-- Het aanvinkvakje waarmee de gebruiker zijn andere sessies afsluit. -->
<#macro logoutOtherSessions>
    <nldd-checkbox-field id="logout-sessions" name="logout-sessions" value="on"
                         label="${msg('logoutOtherSessions')}"></nldd-checkbox-field>
</#macro>

<#-- Een regel uitleg boven of onder een formulier. -->
<#macro instruction>
    <nldd-text size="md"><#nested></nldd-text>
</#macro>

<#-- De terugweg naar het inlogscherm. -->
<#macro backToLogin>
    <nldd-link size="sm" href="${url.loginUrl}" start-icon="arrow-left"
               text="${msg('backToLogin')}"></nldd-link>
</#macro>

<#-- De terugweg naar de applicatie waar de gebruiker vandaan kwam. -->
<#macro backToApplication>
    <#if client?? && client.baseUrl?has_content>
        <nldd-link size="sm" href="${client.baseUrl}" start-icon="arrow-left"
                   text="${msg('backToApplication')}"></nldd-link>
    </#if>
</#macro>
