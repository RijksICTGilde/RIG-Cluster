package nl.minbzk.rig.keycloak.mapper;

import org.jboss.logging.Logger;
import org.keycloak.broker.provider.AbstractIdentityProviderMapper;
import org.keycloak.broker.provider.BrokeredIdentityContext;
import org.keycloak.broker.saml.SAMLEndpoint;
import org.keycloak.broker.saml.SAMLIdentityProviderFactory;
import org.keycloak.dom.saml.v2.assertion.AssertionType;
import org.keycloak.models.IdentityProviderMapperModel;
import org.keycloak.models.IdentityProviderSyncMode;
import org.keycloak.models.KeycloakSession;
import org.keycloak.models.RealmModel;
import org.keycloak.models.UserModel;
import org.keycloak.provider.ProviderConfigProperty;
import org.keycloak.saml.common.util.DocumentUtil;
import org.keycloak.saml.processing.core.saml.v2.util.AssertionUtil;
import org.w3c.dom.Document;

import javax.xml.XMLConstants;
import javax.xml.namespace.NamespaceContext;
import javax.xml.xpath.XPath;
import javax.xml.xpath.XPathConstants;
import javax.xml.xpath.XPathFactory;
import java.io.StringReader;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * Unrestricted XPath mapper that can extract ANY value from the SAML assertion
 * using XPath expressions on the full SAML XML document.
 *
 * Unlike the built-in XPathAttributeMapper which is limited to AttributeStatement,
 * this mapper allows XPath queries on the entire SAML response including Subject/NameID.
 */
public class UnrestrictedXPathAttributeMapper extends AbstractIdentityProviderMapper {

    private static final Logger LOGGER = Logger.getLogger(UnrestrictedXPathAttributeMapper.class);

    public static final String PROVIDER_ID = "saml-unrestricted-xpath-idp-mapper";
    public static final String XPATH_EXPRESSION = "xpath.expression";
    public static final String USER_ATTRIBUTE = "user.attribute";

    private static final Pattern NAMESPACE_PATTERN = Pattern.compile("xmlns:(\\w+)=\"(.+?)\"");

    private static final ThreadLocal<XPathFactory> XPATH_FACTORY = ThreadLocal.withInitial(() -> {
        final XPathFactory xPathFactory = XPathFactory.newInstance();
        xPathFactory.setXPathVariableResolver(variableName -> {
            throw new RuntimeException("Variable resolution not supported: " + variableName);
        });
        xPathFactory.setXPathFunctionResolver((functionName, arity) -> {
            throw new RuntimeException("Function resolution not supported: " + functionName);
        });
        return xPathFactory;
    });

    private static final List<ProviderConfigProperty> configProperties = new ArrayList<>();

    static {
        ProviderConfigProperty property;

        property = new ProviderConfigProperty();
        property.setName(XPATH_EXPRESSION);
        property.setLabel("XPath Expression");
        property.setHelpText("XPath expression to extract value from SAML assertion. " +
                "Example for NameID: //*[local-name()='Subject']/*[local-name()='NameID']/text()");
        property.setType(ProviderConfigProperty.STRING_TYPE);
        configProperties.add(property);

        property = new ProviderConfigProperty();
        property.setName(USER_ATTRIBUTE);
        property.setLabel("User Attribute Name");
        property.setHelpText("Name of the user attribute to store the extracted value.");
        property.setType(ProviderConfigProperty.STRING_TYPE);
        configProperties.add(property);

        property = new ProviderConfigProperty();
        property.setName(IdentityProviderMapperModel.SYNC_MODE);
        property.setLabel("Sync Mode Override");
        property.setHelpText("Sync mode for this mapper.");
        property.setType(ProviderConfigProperty.LIST_TYPE);
        property.setOptions(Arrays.asList(
            IdentityProviderSyncMode.IMPORT.toString(),
            IdentityProviderSyncMode.LEGACY.toString(),
            IdentityProviderSyncMode.FORCE.toString()
        ));
        configProperties.add(property);
    }

    @Override
    public String getId() {
        return PROVIDER_ID;
    }

    @Override
    public String[] getCompatibleProviders() {
        return new String[]{SAMLIdentityProviderFactory.PROVIDER_ID};
    }

    @Override
    public String getDisplayCategory() {
        return "Attribute Importer";
    }

    @Override
    public String getDisplayType() {
        return "Unrestricted XPath Attribute Importer";
    }

    @Override
    public String getHelpText() {
        return "Extract any value from SAML assertion using XPath on the full XML document (not limited to AttributeStatement).";
    }

    @Override
    public List<ProviderConfigProperty> getConfigProperties() {
        return configProperties;
    }

    @Override
    public void preprocessFederatedIdentity(KeycloakSession session, RealmModel realm,
                                           IdentityProviderMapperModel mapperModel,
                                           BrokeredIdentityContext context) {
        String attribute = mapperModel.getConfig().get(USER_ATTRIBUTE);
        String xpathExpression = mapperModel.getConfig().get(XPATH_EXPRESSION);

        if (attribute == null || attribute.trim().isEmpty()) {
            LOGGER.warn("User attribute name not configured");
            return;
        }

        if (xpathExpression == null || xpathExpression.trim().isEmpty()) {
            LOGGER.warn("XPath expression not configured");
            return;
        }

        String value = extractValue(context, xpathExpression);
        if (value != null && !value.isEmpty()) {
            context.setUserAttribute(attribute, Arrays.asList(value));
        }
    }

    @Override
    public void updateBrokeredUser(KeycloakSession session, RealmModel realm,
                                  UserModel user, IdentityProviderMapperModel mapperModel,
                                  BrokeredIdentityContext context) {
        String attribute = mapperModel.getConfig().get(USER_ATTRIBUTE);
        String xpathExpression = mapperModel.getConfig().get(XPATH_EXPRESSION);

        if (attribute == null || attribute.trim().isEmpty()) {
            return;
        }

        if (xpathExpression == null || xpathExpression.trim().isEmpty()) {
            return;
        }

        String value = extractValue(context, xpathExpression);
        if (value != null && !value.isEmpty()) {
            user.setSingleAttribute(attribute, value);
        }
    }

    /**
     * Extract value from SAML assertion using XPath on the full XML document.
     */
    private String extractValue(BrokeredIdentityContext context, String xpathExpression) {
        try {
            AssertionType assertion = (AssertionType) context.getContextData().get(SAMLEndpoint.SAML_ASSERTION);
            if (assertion == null) {
                LOGGER.warn("No SAML assertion found in context");
                return null;
            }

            // Convert assertion to XML Document using AssertionUtil
            Document doc = AssertionUtil.asDocument(assertion);
            String xml = DocumentUtil.asString(doc);

            LOGGER.tracef("Applying XPath '%s' to assertion", xpathExpression);

            // Extract namespaces from XML
            Matcher namespaceMatcher = NAMESPACE_PATTERN.matcher(xml);
            Map<String, String> namespaces = new HashMap<>();
            Map<String, String> prefixes = new HashMap<>();
            while (namespaceMatcher.find()) {
                namespaces.put(namespaceMatcher.group(1), namespaceMatcher.group(2));
                prefixes.put(namespaceMatcher.group(2), namespaceMatcher.group(1));
            }

            // Create XPath with namespace context
            XPath xpath = XPATH_FACTORY.get().newXPath();
            xpath.setNamespaceContext(new NamespaceContext() {
                @Override
                public String getNamespaceURI(String prefix) {
                    return namespaces.getOrDefault(prefix, XMLConstants.NULL_NS_URI);
                }

                @Override
                public String getPrefix(String namespaceURI) {
                    return prefixes.get(namespaceURI);
                }

                @Override
                public java.util.Iterator<String> getPrefixes(String namespaceURI) {
                    return null;
                }
            });

            // Execute XPath on the full document
            Object result = xpath.evaluate(xpathExpression, doc, XPathConstants.STRING);
            String value = result != null ? result.toString() : null;

            if (value != null && !value.isEmpty()) {
                LOGGER.debugf("Extracted value: %s", value);
                return value;
            }

            LOGGER.debugf("XPath returned no value");
            return null;

        } catch (Exception e) {
            LOGGER.error("Error applying XPath: " + e.getMessage(), e);
            return null;
        }
    }
}
