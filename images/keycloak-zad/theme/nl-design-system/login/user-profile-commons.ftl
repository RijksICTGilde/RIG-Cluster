<#--
  De velden van het user profile, in NLDD-componenten. Keycloak bepaalt per attribuut het
  label, de hulptekst, het invoertype en of het verplicht is; dit bestand kiest alleen welk
  component daarbij hoort.
-->
<#macro userProfileFormFields>
    <#assign currentGroup="">

    <#list profile.attributes as attribute>
        <#assign group = (attribute.group)!"">
        <#if group != currentGroup>
            <#assign currentGroup=group>
            <#if currentGroup != "">
                <#assign groupHeaderText = (group.displayHeader!"") != ""
                    ?then(advancedMsg(group.displayHeader), group.name!"")>
                <nldd-title size="5" heading-level="2" id="header-${group.name}"
                            text="${groupHeaderText}"
                            <#if (group.displayDescription!"") != "">supporting-text="${advancedMsg(group.displayDescription)}"</#if>></nldd-title>
            </#if>
        </#if>

        <#nested "beforeField" attribute>

        <nldd-form-field label="${advancedMsg(attribute.displayName!'')}"
                         <#if !attribute.required>optional</#if>>
            <@inputFieldByType attribute=attribute/>
            <#if attribute.annotations.inputHelperTextBefore??>
                <nldd-form-field-help-text>${kcSanitize(advancedMsg(attribute.annotations.inputHelperTextBefore))?no_esc}</nldd-form-field-help-text>
            </#if>
            <#if attribute.annotations.inputHelperTextAfter??>
                <nldd-form-field-help-text>${kcSanitize(advancedMsg(attribute.annotations.inputHelperTextAfter))?no_esc}</nldd-form-field-help-text>
            </#if>
            <#if messagesPerField.existsError('${attribute.name}')>
                <nldd-validation-list>
                    <nldd-validation-item id="error-${attribute.name}">${kcSanitize(messagesPerField.get('${attribute.name}'))?no_esc}</nldd-validation-item>
                </nldd-validation-list>
            </#if>
        </nldd-form-field>

        <#nested "afterField" attribute>
    </#list>
</#macro>

<#macro inputFieldByType attribute>
    <#local invalid = messagesPerField.existsError('${attribute.name}')>
    <#switch attribute.annotations.inputType!''>
        <#case 'textarea'>
            <nldd-multi-line-text-field id="${attribute.name}" name="${attribute.name}"
                value="${(attribute.value!'')}"
                <#if attribute.readOnly>readonly</#if>
                <#if attribute.required>required</#if>
                <#if attribute.annotations.inputTypeRows??>rows="${attribute.annotations.inputTypeRows}"</#if>
                <#if attribute.annotations.inputTypeMaxlength??>maxlength="${attribute.annotations.inputTypeMaxlength}"</#if>
                <#if invalid>invalid unmet="error-${attribute.name}"</#if>></nldd-multi-line-text-field>
            <#break>
        <#case 'select'>
        <#case 'multiselect'>
            <#-- nldd-dropdown is een omhulsel om een echte <select>: de browser houdt de
                 verzending, de toetsenbesturing en de toegankelijkheid in handen. -->
            <nldd-dropdown<#if invalid> invalid unmet="error-${attribute.name}"</#if>>
                <select id="${attribute.name}" name="${attribute.name}"
                        <#if attribute.readOnly>disabled</#if>
                        <#if attribute.required>required</#if>
                        <#if attribute.annotations.inputType == 'multiselect'>multiple</#if>>
                    <option value=""></option>
                    <#list attribute.options.options as option>
                        <option value="${option}" <#if attribute.value == option>selected</#if>>${option}</option>
                    </#list>
                </select>
            </nldd-dropdown>
            <#break>
        <#default>
            <nldd-text-field id="${attribute.name}" name="${attribute.name}"
                type="<@inputTagType attribute=attribute/>"
                value="${(attribute.value!'')}"
                <#if attribute.readOnly>readonly</#if>
                <#if attribute.required>required</#if>
                <#if attribute.autocomplete??>autocomplete="${attribute.autocomplete}"</#if>
                <#if attribute.annotations.inputTypePlaceholder??>placeholder="${advancedMsg(attribute.annotations.inputTypePlaceholder)}"</#if>
                <#if invalid>invalid unmet="error-${attribute.name}"</#if>></nldd-text-field>
    </#switch>
</#macro>

<#-- nldd-text-field kent alleen text, email, tel en url; een ander html5-type uit het user
     profile valt terug op text, dat elke waarde aanneemt. -->
<#macro inputTagType attribute>
    <#compress>
        <#local raw = (attribute.annotations.inputType!'text')>
        <#if raw?starts_with("html5-")>
            <#local raw = raw[6..]>
        </#if>
        <#if ["text", "email", "tel", "url"]?seq_contains(raw)>${raw}<#else>text</#if>
    </#compress>
</#macro>
